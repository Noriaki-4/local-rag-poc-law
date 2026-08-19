"""Luna Worker / Reviewerの承認済みJSONLをClassificationRunへ取り込む。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import TypeVar

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent-api"))

from pydantic import BaseModel  # noqa: E402

from app.domains.legal.relation_classification import (  # noqa: E402
    ApprovedAdjudicationRecord,
    RelationAdjudicationCandidatePacket,
    RelationAdjudicationManifest,
    UnresolvedAdjudicationRecord,
)
from app.graph_client import GraphClient  # noqa: E402
from app.legal_adjudication_importer import (  # noqa: E402
    LegalAdjudicationImporter,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


def _load_jsonl(path: Path | None, model_type: type[ModelT]) -> tuple[ModelT, ...]:
    if path is None:
        return ()
    values = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            values.append(model_type.model_validate_json(line))
        except ValueError as error:
            raise ValueError(f"{path}:{line_number}: {error}") from error
    return tuple(values)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--approved", type=Path)
    parser.add_argument("--unresolved", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.publish and not args.apply:
        parser.error("--publish requires --apply")
    if args.approved is None and args.unresolved is None:
        parser.error("at least one of --approved and --unresolved is required")

    packet_bytes = args.packet.read_bytes()
    manifest = RelationAdjudicationManifest.model_validate_json(
        args.manifest.read_text(encoding="utf-8")
    )
    packet_hash = hashlib.sha256(packet_bytes).hexdigest()
    if packet_hash != manifest.source_packet_sha256:
        parser.error("packet SHA-256 does not match manifest")
    packets = _load_jsonl(args.packet, RelationAdjudicationCandidatePacket)
    approved = _load_jsonl(args.approved, ApprovedAdjudicationRecord)
    unresolved = _load_jsonl(args.unresolved, UnresolvedAdjudicationRecord)

    graph = GraphClient()
    try:
        if args.apply:
            graph.ensure_legal_graph_schema()
        report = LegalAdjudicationImporter(graph).run(
            manifest=manifest,
            packets=packets,
            approved_records=approved,
            unresolved_records=unresolved,
            classification_run_id=args.run_id,
            apply=args.apply,
            publish=args.publish,
        )
    finally:
        graph.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
