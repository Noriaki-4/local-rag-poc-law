---
name: legal-relation-adjudicator
description: Classify structurally verified Japanese legal Article pairs into IMPLEMENTS, INCORPORATES, USES_DEFINITION, EXCEPTION_TO, and OVERRIDES with auditable span grounding. Use for offline one-time relation adjudication or blind evaluation; do not use to resolve citation targets or answer legal questions.
---

# Legal Relation Adjudicator

Classify the meaning of an already resolved `REFERENCES` Article pair. Treat the result as an auditable enrichment artifact, not as legal advice.

## Repository boundary

This checked-in skill is specific to `local-rag-poc-law` and is the source of truth
for its offline relation-classification workflow. Resolve `scripts/` and
`references/` relative to this skill directory. Keep its skill version aligned with
the repository's adjudication manifest defaults and rerun Gate 6 whenever the
semantic contract changes.

Before classifying, read [references/classification-contract.md](references/classification-contract.md) completely.

## Input boundary

Accept only a work packet containing:

- a stable candidate key and the complete `basisEdgeIds` set for one directed
  Article pair;
- `referenceSourceArticle`, which contains the reference;
- `referenceTargetArticle`, which the reference resolves to;
- complete Article text split into known spans;
- the exact `referenceOccurrences`; each occurrence's `basisEdgeId`,
  `referenceKind`, source span IDs, Content Unit offsets, and immediate source
  prefix/suffix.

Do not read an evaluation fixture, expected label, previous model output, or answer key during blind evaluation. Do not use a file whose name or candidate label reveals the expected predicate.

If the two endpoints are structurally unverified, obviously inconsistent with the cited law or provision, or missing complete text, return `needs_resolution` and do not classify predicates. Do not choose a replacement Article.

## Workflow

For each candidate independently:

1. Locate every supplied reference occurrence in the source Article. If citation
   text repeats, use its offsets and immediate prefix/suffix to distinguish the
   occurrence. Do not transfer language from another reference in the same Article.
2. Before semantic classification, verify that the supplied target is structurally
   fit for every exact occurrence. Compare any cited paragraph/item and historical or
   amended-version wording with the supplied complete target. If an explicitly
   cited subdivision is absent, or the supplied target is plainly a different
   version with an incompatible subject, return `needs_resolution`; do not turn the
   mismatch into five negative findings.
3. Read both complete Articles and determine the direct roles expressed through the
   supplied occurrences. Multiple occurrences and extraction kinds may support
   different predicates for the same pair; `referenceKind` is a structural hint,
   not a semantic label. For each proposed predicate, identify one supplied
   occurrence that actually bridges the two semantic roles. Language elsewhere in
   either Article may explain those roles, but it cannot replace this occurrence
   bridge. Write a mental endpoint-role inventory before considering physical
   direction: which endpoint delegates, implements, applies, is applied, defines,
   uses, excepts, or is displaced.
4. Evaluate all five predicates independently using their two necessary conditions.
   Apply a predicate's negative examples only to that predicate.
   Independence does not mean ignoring an occurrence already understood for another
   predicate. When an exact read-as occurrence establishes `OVERRIDES`, cross-check
   whether that same occurrence keeps the target rule operative after substitution;
   if it does, evaluate `INCORPORATES` as established too. A command not to apply the
   target, or a separate rule that merely takes priority, does not pass this check.
5. For `USES_DEFINITION`, first check named definitions and explicit scope clauses
   in both endpoints. A definition may project forward to a later Article, so the
   physical reference source may be the semantic definition OBJECT. Unquoted legal
   roles and statuses remain valid when the supplied occurrence clearly carries the
   same role, but classification is precision-first: do not try to enumerate every
   merely plausible role or long indirect scope chain before returning a negative.
6. Evaluate `IMPLEMENTS` independently even when `USES_DEFINITION` is established:
   compare the parent delegation's regulated matter with the subordinate Article's
   supplied matter. More than one predicate may be established.
   Search the complete higher-authority endpoint for every delegation expression,
   not only the target span nearest the occurrence. If a delegation is applied
   through an express `準用` chain stated by the supplied pair, evaluate that
   imported delegation too.
   An incorporated delegation qualifies only when the supplied occurrence's own
   governing sentence connects that exact delegation to the matter supplied by the
   subordinate Article. A nested citation about another provision, period, person,
   or category does not carry a different delegation found elsewhere in the pair.
7. Set each condition and final finding to `established`, `not_established`, or
   `uncertain`. Derive the final finding exactly as specified in the contract.
8. For every established predicate, select existing source and target span IDs that
   directly support it and assign SUBJECT / OBJECT in the predicate's semantic
   direction. Reconstruct direction from roles; never copy physical edge direction.
   For a negative finding, first identify the closest plausible positive evidence
   in the complete pair and state why a necessary condition fails. Do not treat
   `no matching matter`, `no definition`, or `no exception` as analysis unless the
   closest delegation phrase, candidate defined term, or affected rule was checked.
   For `USES_DEFINITION`, distinguish a reusable legal term, role, status, or scoped
   category created by one endpoint from a mere list of requirements, period, or
   delegated slot. If establishing a broad role/status relation, confirm the exact
   concept and occurrence-local bridge; a weak broad positive is worse than leaving
   that optional navigation relation absent.
9. Write exactly one JSON object per candidate. Copy the candidate, Article,
   occurrence, and span IDs used by the output directly from the input; never
   retype, shorten, hash, or reconstruct them. Do not copy `basisEdgeIds` into the
   Worker or Reviewer record; the coordinator resolves the selected occurrence to
   its known basis edge. Before writing, compare each copied identifier for exact
   character-for-character equality. Preserve completed records and resume by
   candidate key after interruption.

After the Worker writes its raw artifact, run
`scripts/bind_single_occurrence_ids.py`. When a candidate has exactly one supplied
reference occurrence, the coordinator owns that mechanical identifier and copies
its hash into every assertion. For candidates with multiple occurrences or physical
basis edges, the Worker's selected hash remains meaning-bearing and the script only
validates it.
The script must not change predicates, conditions, direction, or grounding spans.

Use model reasoning for every semantic decision. Programs may validate known IDs, enum values, completeness, uniqueness, and condition/finding consistency, but must not add, remove, or change a predicate.

## Worker-review-revision mode

For a costly one-time batch, use `gpt-5.6-luna` with high reasoning as both Worker
and Reviewer, but give them different responsibilities. Preserve reasoning quality
and reduce elapsed time through parallel pairs rather than weaker reasoning. Read
[references/review-contract.md](references/review-contract.md) before starting this
mode.

1. The Worker reads each Article pair once and evaluates all five predicates
   together. Do not make five separate model tasks per pair.
2. Run `scripts/bind_single_occurrence_ids.py`, then give the Reviewer the bound
   Worker output. The Reviewer receives the original work packet and the Worker's complete output.
   It checks the Worker's conditions, findings, semantic direction, and grounding,
   then returns `approve` or concrete `request_change` issues. It does not create a
   second blind answer for every candidate.
3. Run `scripts/prepare_revision_packet.py`. It validates the review contract and
   creates a packet containing only `request_change` candidates.
4. The Worker rereads the original text and each review issue, then produces a full
   revised adjudication record for each returned candidate. It must consider the
   critique, but must not copy a proposed correction without verifying it.
5. The Reviewer receives the previous decision, its own issues, and the revised
   decision. It reviews only those changes and returns the final `approve` or
   `request_change` result.

Only Reviewer-approved Worker records may proceed to registration. If the Reviewer
still returns `request_change` after one revision, leave the candidate unresolved
for a higher-capability model or human review. Do not use majority voting, have a
program choose a meaning, or repeat the whole batch.

For a large packet, first run `scripts/shard_work_packet.py`, then run multiple
Worker/Reviewer pairs concurrently as described in the review contract. A candidate
must belong to exactly one shard and one pair. Keep the same Worker identity for its
revision and the same Reviewer identity for its differential re-review. Start the
Reviewer for a shard as soon as that shard's Worker finishes; do not wait for every
Worker shard to finish.

When the operation defines a per-session candidate limit, invoke the sharder with
`--max-candidates-per-shard` instead of deriving a `--shard-count`. The generated
manifest records the model, reasoning effort, session concurrency, context boundary,
single-revision limit, source snapshot, graph schema, prompt version, and candidate
scope hash. Supply those three source values as command options when an older packet
does not contain them. For the current repository's full classification run,
use at most 5 candidates per shard, at most 3 active sessions, `gpt-5.6-luna` for
both roles, and reasoning effort `high`. Do not mix artifacts produced with a
different model or reasoning effort in the same run.

Build label-free live-index packets with `scripts/sample_blind_packet.py`. Use
`--select-basis-jsonl` or repeated `--select-basis-id` when the same audited basis
edges must be regenerated after an extraction fix. Combine already validated
packets with `scripts/assemble_work_packet.py`; it rejects duplicate candidate keys
and basis edge IDs. Neither script supplies or infers semantic labels.

## Output boundary

Write results to the requested JSONL artifact only. Do not update Neo4j, OpenSearch, a classification run, or a published manifest.

Use `adjudicationStatus` as follows:

- `accepted`: all five findings are established or not_established and every established predicate has valid grounding.
- `needs_review`: at least one finding is uncertain or the evidence supports more than one reasonable reading.
- `needs_resolution`: the supplied Article pair or source material is not structurally fit for semantic classification.

Never turn uncertainty into `not_established` merely to complete a batch. Never infer missing legal text from model memory.
Do not use `needs_resolution` merely because the pair is complex, supplementary, or
supports no semantic predicate. A structurally verified complete pair with all five
findings negative is an `accepted` result with an empty assertion list.
