# Review and revision contract

## Roles

- The Worker owns the adjudication record and performs every semantic revision.
- The Reviewer reads the Worker's answer, identifies concrete errors, and decides
  whether that answer can be accepted.
- A program validates identifiers and shapes and routes requested changes. It does
  not infer a predicate or rewrite the Worker's answer.

## Reviewer input

For initial review, provide:

- the original work packet;
- the Worker's complete adjudication record;
- this skill and the classification contract.

For final differential review, also provide:

- the Reviewer's prior issues;
- the Worker's revised adjudication record.

Do not provide an evaluation fixture, expected labels, or hidden answer key.

## Reviewer procedure

For every Worker record:

1. Check structural fitness before reviewing predicates. If the occurrence
   explicitly cites a paragraph/item absent from the supplied target and historical
   or amended-version context shows an incompatible target version, require
   `needs_resolution`. Do not approve five negative findings for that mismatch.
2. Check whether the supplied reference occurrence was interpreted in its actual
   sentence context. If citation text repeats, use its offsets and immediate
   prefix/suffix so meaning is not transferred from another occurrence in the same
   source span.
3. Check both necessary conditions for all five predicates. Review them together in
   one pass; do not launch one model task per predicate.
   For every Worker `not_established`, actively check whether both conditions are
   actually established; absence of a Worker assertion is not evidence that the
   predicate is negative. In particular, for `IMPLEMENTS`, compare the parent
   provision's exact delegation with the subordinate provision's supplied matter.
   For every positive finding, name the supplied occurrence that bridges those two
   roles. Article-wide semantic compatibility without an occurrence-local bridge is
   not sufficient.
4. Independently inventory definition roles in **both** endpoints. For every
   `USES_DEFINITION` check, identify which Article defines the term and which uses
   it, including forward-scope clauses such as `第X条において同じ`. Do not confirm a
   negative merely because the physical target is not the definition source.
   If the source sentence contains multiple citations, map each candidate term to
   the citation that actually defines it. Reject `USES_DEFINITION` when the supplied
   target merely uses or regulates the term and another citation supplies its
   definition.
   Treat a delegated slot and its subordinate enumeration as IMPLEMENTS only unless
   there is a separate reusable term definition. Conversely, always scan
   forward-scope markers such as `第Y条において同じ`; they commonly reverse semantic
   direction.
5. Independently compare delegation matter for `IMPLEMENTS`, even when the Worker
   established another predicate for the pair. Record why the delegated and
   supplied matters match or do not match.
   Scan every span in the higher-authority endpoint for delegation forms such as
   `…で定めるところにより`, `…で定める場合`, and `…で定める事項`. Also inspect an
   express `準用` chain when the supplied pair states that chain. A generic assertion
   that the target delegates nothing is not a completed review.
   For an incorporated delegation, confirm that the occurrence's governing sentence
   connects the exact incorporated delegation to the subordinate matter. Reject a
   chain reconstructed from an unrelated nested citation or category description.
6. Check that the finding follows the condition algebra and that
   `adjudicationStatus` is valid. A complete, structurally verified pair with five
   negatives is `accepted`, not `needs_resolution`.
7. For an established predicate, reconstruct semantic SUBJECT / OBJECT from the
   endpoint roles and check both grounding spans. Do not validate direction by
   comparing it with the physical edge direction.
8. For `INCORPORATES`, identify the grammar that makes the target rule operative in
   the current source situation. Accept direct transitional application such as
   `これらの規定の例により`; reject `準用する` that only modifies a person, a
   category, or a third provision.
   Also reject a reverse reconstruction where the occurrence only says that the
   current source rule is applied in the target (`target条において準用するsource条の
   規定`). The target's independent application sentence is not the supplied
   occurrence under review.
9. For `EXCEPTION_TO` and `OVERRIDES`, identify the target rule or legal effect and
   decide whether the source narrows/terminates that effect, gives a competing rule
   priority, or both. A loss of status or effect is not by itself an override. For a
   transitional cutover, separately check post-effective application, pre-effective
   exclusion, and preservation of the old rule. For a table, verify the exact row's
   subject matter against the supplied target rather than relying on the preamble.
   A subordinate rule that only fills in a delegated condition for an alternative
   already authorized by the target is implementation, not an exception, unless it
   independently narrows the target rule or effect.
   Treat an exact read-as row as explicit priority when it forces replacement words
   for the incorporated target rule. Treat target-specific `適用しない` as priority
   when a special regime directly controls that target's applicability. Do not apply
   either rule to a mere table preamble, unrelated row, or status termination.
10. Check grounding as a physical occurrence anchor. Semantic language may continue
    in another span of the complete Article; do not require the Worker to replace
    the assertion's source span with a span outside the occurrence's
    `sourceSpanIds`, and do not remove a supported predicate for that reason.
    For `USES_DEFINITION`, confirm that the other endpoint textually uses the term;
    wholesale incorporation of the defining Article does not count as use of every
    definition it contains.
11. Return `approve` when there is no concrete defect. Otherwise return
   `request_change` and identify every defect that the Worker must reconsider.

The Reviewer may recommend a correction, but it must explain the textual reason.
The Reviewer does not directly replace the Worker record.

## Review JSONL record

```json
{
  "candidateKey": "known candidate key",
  "reviewStatus": "approve | request_change",
  "predicateChecks": {
    "IMPLEMENTS": {
      "workerFinding": "established | not_established | uncertain",
      "reviewConclusion": "confirmed | change_required",
      "note": "Concise reason based on both necessary conditions"
    },
    "INCORPORATES": {},
    "USES_DEFINITION": {},
    "EXCEPTION_TO": {},
    "OVERRIDES": {}
  },
  "issues": [
    {
      "predicate": "IMPLEMENTS",
      "problemType": "condition | finding | direction | grounding",
      "critique": "What is wrong and why, grounded in the supplied Articles",
      "recommendedAction": "What the Worker should reconsider",
      "supportingSpanIds": ["known span ID"]
    }
  ]
}
```

Rules:

- `predicateChecks` must contain all five predicates, including Worker negatives.
- `workerFinding` must exactly copy that predicate's finding from the Worker record.
- `confirmed` means the finding, direction, and grounding require no change.
- `change_required` requires at least one issue for the same predicate.
- `approve` requires an empty `issues` list.
- `request_change` requires at least one issue.
- `approve` is allowed only when all five checks are `confirmed`.
- `request_change` is required when any check is `change_required`.
- `predicate` must be one of the five classification predicates.
- `supportingSpanIds` is optional. When present, every value must be a known span ID
  from the supplied Article pair.
- `critique` and `recommendedAction` must be specific enough for the Worker to redo
  the semantic analysis; a label alone is insufficient.
- For `USES_DEFINITION`, the check note must name the defining endpoint and using
  endpoint and the candidate term. For a negative, it must name the closest
  candidate defined term found in either endpoint and explain why the governing
  source sentence does not use it. `No defining endpoint` alone is insufficient.
  The note must also say which supplied occurrence carries the scope. A provision
  that constitutes a reusable role or status may define its scope without a quoted
  short name; a mere period, requirement list, or delegated slot does not.
- For `IMPLEMENTS`, the check note must compare the delegated matter and supplied
  matter. For a negative, it must quote or identify the closest delegation phrase
  found in the complete higher-authority Article and explain the matter mismatch. If
  there is no delegation phrase anywhere, say that every span was scanned. A generic
  statement that delegation is absent is insufficient.
  When relying on an incorporated delegation, the note must identify how the same
  occurrence links that delegation to the supplied matter; a wrapper Article or
  unrelated nested citation is insufficient.
- For `EXCEPTION_TO` and `OVERRIDES`, the notes must name the affected target rule
  or legal effect and distinguish narrowing/termination from priority. In a table,
  name the exact row subject and the supplied target subject.

## Revision boundary

The revision packet contains only candidates with `request_change`:

```text
originalCandidate  Original Article pair and reference occurrence
previousDecision   Worker's answer being reviewed
reviewFeedback     Reviewer's concrete issues
```

The Worker returns a complete replacement adjudication record for each revision
candidate. Unreviewed candidates remain unchanged. A program may merge only records
that the final Reviewer has approved.

During final differential review, reread the original pair as well as the prior
issue and revised decision. The prior critique is not an answer key. If it was
mistaken, do not approve a revision merely for following it; return a concrete
`request_change` explaining the remaining inconsistency. The single-revision limit
still applies.

## Parallel pairs

Partition a large packet before starting model work:

```text
Coordinator: deterministic sharding only
    ├─ Shard 1 → Worker A → Reviewer A → requested changes → Worker A → Reviewer A
    ├─ Shard 2 → Worker B → Reviewer B → requested changes → Worker B → Reviewer B
    └─ Shard 3 → Worker C → Reviewer C → requested changes → Worker C → Reviewer C
```

The arrows inside each row are dependencies; the rows run concurrently. A Reviewer
may start as soon as its own Worker artifact is complete. The Coordinator may queue
more shards than concurrent pairs and assign the next shard when a pair finishes.

Operational invariants:

- One candidate appears in exactly one shard. Never use majority voting across pairs.
- Each Worker evaluates all five predicates together for every assigned candidate.
- Reviewer A sees Worker A's output, but never reviews another pair's in-flight shard.
- A revision goes back to the same Worker that produced the reviewed answer.
- A differential re-review goes back to the same Reviewer that issued the critique.
- Use a fresh pair context for the next shard so earlier Article text does not grow
  the context indefinitely.
- Write artifacts under a unique shard ID. No two agents write the same file.
- The Coordinator validates manifest coverage, candidate uniqueness, review shapes,
  and final approval status. It performs no semantic classification.

Choose the number of active pairs from the available agent slots. Pair count controls
concurrency; shard count controls resumable work size. They need not be equal.
