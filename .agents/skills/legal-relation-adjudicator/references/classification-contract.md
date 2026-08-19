# Classification contract

## Physical input direction

- `referenceSourceArticle`: the Article containing the supplied `REFERENCES` occurrence.
- `referenceTargetArticle`: the Article selected by that occurrence.
- This physical direction does not determine semantic SUBJECT / OBJECT direction.
- One candidate contains all verified occurrences joining the same directed Article
  pair. Other citations outside `referenceOccurrences` are not being classified.
- Each occurrence carries its physical `basisEdgeId` and extraction
  `referenceKind`. Those fields preserve provenance; `referenceKind` does not decide
  a semantic predicate.
- `sourceStart` / `sourceEnd` identify the occurrence inside its Content Unit, and
  `sourcePrefix` / `sourceSuffix` contain its immediate original context. Use these
  fields to distinguish repeated identical citation text, including repetitions in
  one source span.

## Finding algebra

Each predicate has two necessary conditions.

- Both conditions `established` -> predicate finding `established`.
- Either condition `not_established` -> predicate finding `not_established`.
- Otherwise -> predicate finding `uncertain`.

Predicates are independent. Do not force exactly one predicate to be established, and do not establish a second predicate merely because another one is established.
An exclusion stated for one predicate is not an exclusion for another. In
particular, `第X条に規定するY` is insufficient for INCORPORATES but may be
direct evidence for USES_DEFINITION.

## Predicates

### IMPLEMENTS

Semantic direction: SUBJECT is the delegating parent provision; OBJECT is the subordinate provision that supplies the delegated detail.

Necessary conditions:

1. `explicitDelegation`: the target parent provision expressly delegates the same matter to a Cabinet Order, ministerial ordinance, Cabinet Office ordinance, or another subordinate instrument.
2. `sameMatterImplementation`: the source subordinate provision concretely supplies that same delegated matter.

A lower provision merely citing a parent Article, using its definition, or applying
it mutatis mutandis is not enough. Check both texts and the authority levels.
However, predicates are independent: if the subordinate Article both uses a parent
definition and supplies a detail expressly delegated by the parent for the same
regulated matter, establish both `USES_DEFINITION` and `IMPLEMENTS`.

Before making `IMPLEMENTS` negative, compare these four items explicitly:

1. the parent provision's delegated matter;
2. the instrument or authority to which it delegates;
3. the subordinate provision's regulated matter; and
4. whether the supplied occurrence connects those same matters.

Generic phrases such as `内閣府令で定める` establish condition 1 only when the
delegated noun, category, threshold, procedure, or other matter matches what the
subordinate Article supplies. Do not reject `IMPLEMENTS` solely because another
predicate is already established.

Bind the delegation comparison to the supplied occurrence. A nested citation that
describes a third provision's application, or a citation embedded in a category
description, does not transfer a delegation found elsewhere in the Article to this
pair. The occurrence must connect the parent delegation and the subordinate supply
for the same matter.

Scan the complete higher-authority Article for delegation language, including
`…で定めるところにより`, `…で定める場合`, and `…で定める事項`. A source Article
that specifies the delegated method, case, item, threshold, or procedure can satisfy
condition 2 even when it also quotes a definition from the parent. Delegation may
also reach the supplied source through an explicit incorporation chain: if the
higher-authority endpoint expressly applies another paragraph containing the
delegation and the supplied occurrence identifies that same chain and matter, treat
the incorporated delegation as part of the higher-authority endpoint's applicable
rule. Do not infer an unseen chain from memory.

For an incorporated delegation, require an occurrence-local bridge. The governing
sentence around the supplied occurrence must both identify the incorporated
delegation and use it to qualify the exact matter supplied by the subordinate
Article. A citation that identifies a period, person, document, or other category by
describing how a third provision applies does not import a different delegation
found elsewhere in either Article.

### INCORPORATES

Semantic direction: SUBJECT is the provision that applies or reads in another rule; OBJECT is the incorporated rule.

Necessary conditions:

1. `explicitApplicationLanguage`: the source uses direct application language for
   this target, such as `準用する`, `読み替えて適用する`, or `この規定の例により`.
2. `targetRuleApplied`: the target Article's rule itself is applied to the source situation.

Read the occurrence in its governing sentence context within the same source Article. A
blanket or range-based `準用する` statement followed by `この場合において` and a
target-specific read-as clause establishes INCORPORATES when the target is within that
incorporated set. The supplied occurrence may therefore appear in the read-as clause
rather than in the preceding blanket statement. This is context for the same
incorporation, not a transfer of meaning from an unrelated reference.

Do not establish this for `第X条に規定する`, `第X条の場合`, `第X条の規定による`, a table cell that merely names a provision, or a nested statement saying that some third provision is applied elsewhere. The application language must govern this exact source-target pair. In particular, `準用する` inside a relative clause that merely describes a person, association, or third provision does not make the target rule operative in the current source situation.

The supplied occurrence must itself be the operative application statement for
the asserted INCORPORATES relation. If Article A merely says `B条において準用する
A条の規定` while listing or describing a category, that is a backward description
of an application made by B; it is not a direct INCORPORATES assertion generated by
the occurrence in A. Do not recover the reverse assertion merely by finding in the
other endpoint that B independently applies A.

A transitional source can establish INCORPORATES without the word `準用` when it
allows or requires conduct under the target rule before or after a cutover through
language such as `これらの規定の例により`. Mere reference to conduct `の規定に
よる` remains insufficient unless the source makes that target rule operative.

### USES_DEFINITION

Semantic direction: SUBJECT uses the term; OBJECT defines its meaning or scope.

Necessary conditions:

1. `oneEndpointDefinesTerm`: one supplied Article defines the referenced term or
   establishes its scope through language such as `Xとは`, `Xをいう`, `以下…という`,
   `第X条において同じ`, or an equivalent scope definition.
2. `otherEndpointUsesSameTerm`: the other supplied Article uses that same term, and
   the supplied occurrence establishes the definition's scope linkage between the
   two Articles.

Evaluate this predicate by explicitly matching the term on both sides before
returning `not_established`:

1. Inventory terms whose meaning or local scope **each endpoint** establishes,
   including parenthetical forms such as `以下この条において「Y」という`, direct
   forms such as `第X条に規定するY`, and forward-scope forms such as `第X条において
   同じ` or `以下第X条までにおいて同じ`.
2. Identify the defining endpoint and the using endpoint. Check that the supplied
   occurrence, rather than another citation in the Article, connects the same term
   and scope.
3. When both conditions are present, establish `USES_DEFINITION` even though
   `に規定する` would not establish INCORPORATES.
4. Assign semantic direction as SUBJECT = using Article and OBJECT = defining
   Article. This may be the reverse of the physical `REFERENCES` edge when a
   definition clause points forward to later Articles where the term also applies.

Apply these boundary rules before establishing the predicate:

- A parent phrase such as `内閣府令で定めるX` and a subordinate Article that
  enumerates or supplies X establish IMPLEMENTS for that delegated slot. They do
  not also establish USES_DEFINITION from the same slot alone. Add
  USES_DEFINITION only when one endpoint independently creates a reusable meaning
  or scope for a term and the other endpoint uses that term through this occurrence.
- Shared statutory categories, repeated nouns, implementation criteria, and a
  subordinate Article's enumeration are not definitions by themselves.
- `第X条…に規定するY` is positive evidence only when Article X actually defines
  or fixes the scope of Y. If it merely regulates Y, condition 1 fails.
- Always scan for forward-scope markers such as `第Y条において同じ` and
  `以下第Y条までにおいて同じ`. In that pattern the current Article defines the
  term and Article Y uses it, so semantic direction is normally the reverse of the
  physical reference.
- The using endpoint must itself use the defined term through the supplied
  occurrence. Applying another Article as a whole does not make the applying
  endpoint a user of every term defined inside that Article, and an endpoint cannot
  satisfy both definition and use merely by referring to its own defined term while
  describing a reverse application by the other endpoint.

Definitions include named legal statuses introduced by forms such as
`以下「X」という` after a rule creates or designates that status. The using term
may appear in the governing sentence around the supplied Article citation rather
than inside the citation characters themselves. Before a negative finding, list
every plausible defined term from both complete Articles and compare it with the
terms in that governing sentence.

A reusable legal role or category may also be scope-setting without a quoted short
name. For example, an Article may constitute a role by authorizing or designating a
person under a specified paragraph, and the other endpoint may use that role as
`第X条第Y項の…者`. This can satisfy condition 1 only when the target provision
itself determines who has that role and the supplied occurrence is the scope
bridge. It does not make every person, period, requirement set, or regulated subject
a definition.

Forms such as `第X条第Y項の委託を受けた者`, `許可を受けた者`, or `登録を受けた者`
can therefore be positive when Article X paragraph Y itself creates or identifies
that reusable role. Do not require a quoted short name in those cases. Compare the
role-forming act in the defining endpoint with the exact role used around the
supplied occurrence.

Conversely, `第X条に規定する期間`, `前条各号の要件`, or similar
cross-references are not definitions merely because they delimit a result. Require
either a reusable term, role, or status, or an express scope clause that makes the
same concept operative in the using endpoint. When several citations occur in the
same sentence, the selected occurrence must be the one that carries that scope.

When the governing sentence contains multiple citations, bind each definition to
the citation that actually introduces it. Do not attribute a term defined through a
second citation to the supplied target occurrence. An endpoint's mere use, listing,
or regulation of a term does not make it the definition source; the endpoint must
establish that term's meaning or scope, and the supplied occurrence must carry that
definition to the other endpoint.

A forward-scope clause does not make every reverse citation between the same two
Articles a definition bridge. When the supplied occurrence is an independent rule
reference such as `前条に該当する場合`, require the defined term to be used in that
occurrence's governing sentence or exact structural item. A use elsewhere in the
Article is insufficient. Conversely, when the defining endpoint expressly projects
the term to a particular later item and that same item contains both the supplied
reverse citation and the term use, the occurrence can carry the scope linkage.

An endpoint that only states a right, obligation, requirement, procedure, category,
or legal effect is not a definition source.

### EXCEPTION_TO

Semantic direction: SUBJECT states the exception; OBJECT states the general rule being narrowed.

Necessary conditions:

1. `targetContainsAffectedRule`: the target contains the same rule or legal effect that the source narrows.
2. `citationDirectlyLimitsTargetRule`: the exception or exclusion language in the source directly limits that target rule through this occurrence.

The mere presence of `ただし`, `除く`, or `この限りでない` is insufficient when the target is only a definition source or when the exception limits another rule in the source Article.

An exception need not use those markers. It also includes a condition that narrows
the persons, cases, time period, or continuing legal effect of the target rule. For
example, a source rule stating that a status or designation created under the target
loses effect upon specified events narrows the target's legal effect. A transitional
rule that makes the target applicable only from a stated date creates a temporal
exception for the excluded pre-effective scope when the supplied occurrence governs
that exact target.

Do not classify a subordinate provision as an exception when it only supplies the
criteria, method, or detail for an alternative that the target rule itself expressly
authorizes through delegation. That realizes a branch already contained in the
target and is evaluated under `IMPLEMENTS`. `EXCEPTION_TO` additionally requires
the source to narrow a target rule or effect beyond merely filling in its delegated
condition.

### OVERRIDES

Semantic direction: SUBJECT is the priority rule; OBJECT is the rule whose application is displaced or modified.

Necessary conditions:

1. `explicitPriorityOverTarget`: the source expressly takes priority over this target, for example through `第X条の規定にかかわらず`.
2. `targetApplicationModified`: the source excludes or modifies how the target rule applies.

Explicit priority is functional, not limited to the word `かかわらず`. It also
includes a target-specific command that makes an incorporated rule apply only after
an exact read-as substitution (`…と読み替えるものとする`) or directly commands
that `第X条は適用しない` for a special regime. For a read-as table, the exact row
must identify the target's regulated subject and replacement. For non-application,
the source must directly control this target's applicability; a mere loss or
termination of a status remains only a possible `EXCEPTION_TO`.

An additional requirement, separate legal effect, incorporation, or definition use
is not an override. A transitional rule may establish more than one predicate: an
express application of another rule can be `INCORPORATES`, a temporal carve-out can
be `EXCEPTION_TO`, and wording such as `なお従前の例による` can be `OVERRIDES`
when it expressly displaces the newly applicable rule for the stated pre-effective
scope. Confirm each predicate from its own two conditions.

Distinguish priority from termination. A rule that merely ends a designation,
status, permission, obligation, or other legal effect upon stated events is normally
an `EXCEPTION_TO`, not an `OVERRIDES`, unless the source also identifies a competing
rule that takes priority. Conversely, a transitional sentence that both applies the
new target after a cutover and preserves the old rule before it may establish
`INCORPORATES`, `EXCEPTION_TO`, and `OVERRIDES` together.

For a read-as table, the preamble alone does not prove that every extracted Article
number is overridden. Locate the exact row/column occurrence and compare the target
Article's regulated subject with the words replaced in that row. If flattened table
text or a different historical version makes the supplied current target's subject
matter inconsistent, do not establish `OVERRIDES` for that pair.

## Grounding

For every established predicate:

- choose one supplied `referenceOccurrenceHash`;
- choose `referenceSourceSupportingSpanId` from that occurrence's known `sourceSpanIds`;
- choose `referenceTargetSupportingSpanId` from the target Article's known spans;
- choose SUBJECT and OBJECT only from the two supplied Article IDs;
- do not reproduce or invent a span ID.

Grounding span names remain physical: source support must come from the reference source even when the semantic SUBJECT is the target.

`referenceSourceSupportingSpanId` is the physical anchor of the supplied occurrence,
not a container for every word needed for semantic reasoning. The model may use
other spans in the same complete source Article to understand a governing sentence
or transitional rule, but it must still select the occurrence's known source span
for the assertion. Do not drop an otherwise established predicate merely because
its priority or application language continues in an adjacent source span.

## Structural fitness preflight

Return `needs_resolution` before evaluating predicates when the supplied pair is
not fit for semantic classification. This includes an occurrence that explicitly
cites a paragraph or item absent from the supplied target, together with historical
or amended-version wording or a clear subject-matter mismatch showing that the
current consolidated Article is not the cited version. Complexity, supplementary
location, or an empty predicate set alone are not structural defects.

## JSONL record

```json
{
  "candidateKey": "known candidate key",
  "adjudicationStatus": "accepted | needs_review | needs_resolution",
  "predicateAssessments": {
    "IMPLEMENTS": {
      "firstCondition": "established | not_established | uncertain",
      "secondCondition": "established | not_established | uncertain",
      "finding": "established | not_established | uncertain"
    },
    "INCORPORATES": {},
    "USES_DEFINITION": {},
    "EXCEPTION_TO": {},
    "OVERRIDES": {}
  },
  "assertions": [
    {
      "proposedPredicate": "IMPLEMENTS",
      "referenceOccurrenceHash": "known hash",
      "subjectArticleId": "known Article ID",
      "objectArticleId": "known Article ID",
      "referenceSourceSupportingSpanId": "known source span ID",
      "referenceTargetSupportingSpanId": "known target span ID"
    }
  ],
  "note": "Short explanation of ambiguity only; omit for accepted records"
}
```

The condition names are predicate-specific in meaning even though the compact JSON
uses `firstCondition` and `secondCondition`. Preserve the condition order listed
above. For `USES_DEFINITION`, the first condition is that either endpoint defines
the term and the second is that the other endpoint uses it through the supplied
scope linkage; neither condition presumes physical source/target direction.
