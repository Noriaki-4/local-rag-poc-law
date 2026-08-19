"""Codex Luna成果物の検証importとpublishを行う。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable

from .domains.legal.adjudication_import import (
    build_adjudication_import_batch,
    classification_run_from_adjudication_manifest,
)
from .domains.legal.relation_classification import (
    ApprovedAdjudicationRecord,
    RelationAdjudicationCandidatePacket,
    RelationAdjudicationManifest,
    UnresolvedAdjudicationRecord,
)
from .legal_relation_classification_job import audit_classification_materialization


class LegalAdjudicationImporter:
    """入力契約を検証し、候補単位transactionへ渡す。"""

    def __init__(self, graph_client: Any) -> None:
        self.graph = graph_client

    def run(
        self,
        *,
        manifest: RelationAdjudicationManifest,
        packets: Iterable[RelationAdjudicationCandidatePacket],
        approved_records: Iterable[ApprovedAdjudicationRecord],
        unresolved_records: Iterable[UnresolvedAdjudicationRecord],
        classification_run_id: str | None = None,
        apply: bool = False,
        publish: bool = False,
        processed_at: datetime | None = None,
    ) -> dict[str, Any]:
        if publish and not apply:
            raise ValueError("publish requires apply")
        packet_values = tuple(packets)
        run = classification_run_from_adjudication_manifest(
            manifest,
            packet_values,
            classification_run_id=classification_run_id,
        )
        batch = build_adjudication_import_batch(
            packet_values,
            approved_records,
            unresolved_records,
            classification_run_id=run.classification_run_id,
            processed_at=processed_at or datetime.now(UTC),
        )
        assertion_count = sum(
            len(values) for values in batch.assertions_by_candidate.values()
        )
        if not apply:
            return {
                "classificationRunId": run.classification_run_id,
                "sourceSnapshotId": run.source_snapshot_id,
                "inputCount": run.input_count,
                "importCandidateCount": len(batch.checkpoints),
                "assertionCount": assertion_count,
                "dryRun": True,
                "published": False,
            }

        persisted = self.graph.create_or_resume_classification_run(
            run.model_dump(by_alias=True, mode="json")
        )
        self._validate_persisted_run(run.model_dump(by_alias=True, mode="json"), persisted)
        saved_count = 0
        skipped_count = 0
        try:
            for checkpoint in batch.checkpoints:
                assertions = batch.assertions_by_candidate[checkpoint.candidate_key]
                saved = self.graph.save_classification_checkpoint(
                    checkpoint=checkpoint.model_dump(by_alias=True, mode="json"),
                    assertions=[
                        assertion.model_dump(by_alias=True, mode="json")
                        for assertion in assertions
                    ],
                )
                if saved:
                    saved_count += 1
                else:
                    skipped_count += 1
        except RuntimeError as exc:
            if "conflict" in str(exc).lower():
                self.graph.fail_classification_run(
                    run.classification_run_id,
                    error_code="adjudication_import_conflict",
                )
            raise

        published = False
        if publish:
            materialization = self.graph.classification_run_materialization(
                run.classification_run_id
            )
            candidates = tuple(packet.to_candidate() for packet in packet_values)
            violations = audit_classification_materialization(
                materialization, candidates
            )
            if violations:
                raise RuntimeError(
                    "classification publish audit failed: " + ", ".join(violations)
                )
            run_state = dict(materialization.get("run") or {})
            if int(run_state.get("failedCount") or 0):
                raise RuntimeError("classification run contains failed checkpoints")
            self.graph.publish_classification_run(
                run.classification_run_id,
                published_at=datetime.now(UTC),
            )
            published = True
        return {
            "classificationRunId": run.classification_run_id,
            "sourceSnapshotId": run.source_snapshot_id,
            "inputCount": run.input_count,
            "importCandidateCount": len(batch.checkpoints),
            "assertionCount": assertion_count,
            "savedCount": saved_count,
            "skippedCount": skipped_count,
            "dryRun": False,
            "published": published,
        }

    @staticmethod
    def _validate_persisted_run(
        expected: dict[str, Any], persisted: dict[str, Any]
    ) -> None:
        immutable_fields = (
            "classificationRunId",
            "sourceSnapshotId",
            "graphSchemaVersion",
            "provider",
            "model",
            "reviewerModel",
            "promptVersion",
            "skillVersion",
            "reasoningEffort",
            "candidatesPerModelCall",
            "inputCount",
            "scopeHash",
        )
        if any(persisted.get(field) != expected.get(field) for field in immutable_fields):
            raise RuntimeError("persisted classification run conflicts with manifest")
        if persisted.get("phase") != "building":
            raise RuntimeError("persisted classification run is not importable")


__all__ = ["LegalAdjudicationImporter"]
