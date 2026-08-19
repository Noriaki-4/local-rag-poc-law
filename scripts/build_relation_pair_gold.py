#!/usr/bin/env python3
"""旧edge正解と人手確認済みoverrideからArticleペア正解を作る。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent-api"))

from app.domains.legal.relation_classification import (  # noqa: E402
    RelationAdjudicationCandidatePacket,
    WorkerAdjudicationRecord,
    validate_worker_adjudication,
)

PREDICATES = (
    "IMPLEMENTS",
    "INCORPORATES",
    "USES_DEFINITION",
    "EXCEPTION_TO",
    "OVERRIDES",
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _index(
    records: list[dict[str, Any]], key: str, *, label: str
) -> dict[str, dict[str, Any]]:
    result = {str(record[key]): record for record in records}
    if len(result) != len(records):
        raise ValueError(f"duplicate {key} in {label}")
    return result


def _atomic_create(path: Path, data: bytes) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _without_legacy_identity(
    decision: dict[str, Any], *, candidate_key: str
) -> dict[str, Any]:
    migrated = dict(decision)
    migrated["candidateKey"] = candidate_key
    migrated.pop("basisEdgeId", None)
    return migrated


def _expand_pair_override(override: dict[str, Any]) -> dict[str, Any]:
    """人が決めた三値列をWorker契約へ機械的に展開する。"""

    compact = override.get("predicateAssessments")
    if not isinstance(compact, dict) or set(compact) != set(PREDICATES):
        raise ValueError(
            f"pair override must assess all predicates: {override.get('candidateKey')}"
        )
    assessments: dict[str, dict[str, str]] = {}
    for predicate in PREDICATES:
        values = compact[predicate]
        if not isinstance(values, list) or len(values) != 3:
            raise ValueError(
                f"pair override assessment must have three values: "
                f"{override.get('candidateKey')} {predicate}"
            )
        assessments[predicate] = {
            "firstCondition": values[0],
            "secondCondition": values[1],
            "finding": values[2],
        }
    return {
        "candidateKey": override["candidateKey"],
        "adjudicationStatus": override["adjudicationStatus"],
        "predicateAssessments": assessments,
        "assertions": override.get("assertions", []),
        **({"note": override["note"]} if override.get("note") else {}),
    }


def build_pair_gold_records(
    packets: list[RelationAdjudicationCandidatePacket],
    legacy_records: list[dict[str, Any]],
    override_records: list[dict[str, Any]],
) -> tuple[tuple[WorkerAdjudicationRecord, ...], dict[str, int]]:
    """人手overrideを優先し、残る単一edgeだけを旧goldから移行する。"""

    legacy_by_basis = _index(legacy_records, "basisEdgeId", label="legacy audit")
    overrides = _index(override_records, "candidateKey", label="pair overrides")

    output: list[WorkerAdjudicationRecord] = []
    used_overrides: set[str] = set()
    singleton_override_count = 0
    multi_edge_override_count = 0
    for packet in packets:
        override = overrides.get(packet.candidate_key)
        if override is not None:
            raw_decision = _expand_pair_override(override)
            used_overrides.add(packet.candidate_key)
            if len(packet.basis_edge_ids) == 1:
                singleton_override_count += 1
            else:
                multi_edge_override_count += 1
        elif len(packet.basis_edge_ids) == 1:
            selected_legacy = [
                legacy_by_basis[basis_id]
                for basis_id in packet.basis_edge_ids
                if basis_id in legacy_by_basis
                and legacy_by_basis[basis_id].get("semanticDecision") is not None
            ]
            if len(selected_legacy) != 1:
                raise ValueError(
                    "singleton candidate must map to exactly one legacy gold row: "
                    f"{packet.candidate_key}"
                )
            raw_decision = _without_legacy_identity(
                dict(selected_legacy[0]["semanticDecision"]),
                candidate_key=packet.candidate_key,
            )
        else:
            raise ValueError(
                f"multi-edge candidate requires a reviewed override: {packet.candidate_key}"
            )

        decision = WorkerAdjudicationRecord.model_validate(raw_decision)
        validate_worker_adjudication(packet.to_candidate(), decision)
        output.append(decision)

    unused = set(overrides).difference(used_overrides)
    if unused:
        raise ValueError(f"unused pair overrides: {sorted(unused)}")

    ordered = tuple(sorted(output, key=lambda item: item.candidate_key))
    return ordered, {
        "candidateCount": len(ordered),
        "singletonMigratedCount": len(ordered) - len(used_overrides),
        "pairOverrideCount": len(used_overrides),
        "singletonOverrideCount": singleton_override_count,
        "multiEdgeOverrideCount": multi_edge_override_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--legacy-audit", type=Path, required=True)
    parser.add_argument("--pair-overrides", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    packets = [
        RelationAdjudicationCandidatePacket.model_validate(record)
        for record in _load_jsonl(args.packet)
    ]
    output, stats = build_pair_gold_records(
        packets,
        _load_jsonl(args.legacy_audit),
        _load_jsonl(args.pair_overrides),
    )

    data = "".join(
        json.dumps(
            record.model_dump(by_alias=True, mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
        for record in sorted(output, key=lambda item: item.candidate_key)
    ).encode("utf-8")
    _atomic_create(args.output.resolve(), data)
    print(
        json.dumps(
            {
                **stats,
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
