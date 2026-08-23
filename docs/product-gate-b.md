# Product Gate B protocol and author-corpus audit

Product Gate B is a human product decision about real manuscript revision. It is
not satisfied by synthetic fixtures, model-written revisions, metadata-only
commits, or a green verifier alone.

The gate is run only after Phase 5 (versioning and Claim lineage) and Phase 6
(Finding Resolution). Phase 7 must not use an uncompleted Gate B as evidence
that lineage or resolution is usable.

## Required evidence

Use two or three author-owned projects with at least two genuine manuscript
versions each. The corpus must contain:

- a human decision for every adjacent-version lineage analysis;
- at least one human-confirmed `split` or `merged` lineage relation across the
  corpus;
- at least one Finding Resolution decision produced by re-running the original
  Rule or Perspective Lens;
- one human assessment for every project; and
- an explicit human `pass`, `fail`, or `defer` decision.

The assessment records whether lineage was reasonable, split/merge worked,
Findings followed the right descendants, resolved Findings stopped reappearing,
unresolved Findings persisted, and the author can still explain why a revision
was made. No score is calculated.

## 2026-08-23 author-corpus audit

The author identified `UYMIDGameStudio/billcharles-blog` as the source
repository. The following history was inspected before creating any Gate B
workspace. Commit identifiers below are evidence locators; manuscript bytes
remain in the source repository and are not copied into this document.

### `the-structural-stand-in.md`

- Initial manuscript: `7af3f3ae7ff1233ffbbd6ca766d9e4ae372d2565`
- Follow-up edit: `fd99330b1953ad4cba2347e10d6884de39cbb4df`
- Observed change: one line replaced. The edit removes a transition and the
  closing sentence inviting the reader to decide whether the mechanism is good
  or bad; the surrounding argument is otherwise unchanged.
- Gate status: a real but too narrow revision to carry one of the two required
  end-to-end cases by itself. It may be retained as a useful `unchanged` /
  `modified` regression example after a substantive V2 exists.

### `the-dynamic-dialectic-of-knowledge-systems-on-change-and-invariance-in-theoretical-identity.md`

- Initial upload: `ca67cfea036d3c6f4d1fc678e61e4786c3da786a`
- Follow-up edit: `3f9ecf4758a14fa2ec78decb68c3bedf32e2a3b2`
- Observed change: frontmatter/title/slug cleanup; the manuscript argument is
  unchanged.
- Gate status: excluded. Metadata changes are deterministic document changes,
  not semantic argument revisions.

### `diachronic-continuity-and-argumentative-responsibility-in-knowledge-migration.md`

- Published manuscript: `2c03434e79b8b51b4547e1aaf33182fbedd8db4d`
- Observed history: only the initial manuscript commit exists.
- Gate status: excluded until an author revision exists.

The current Product Gate A workspaces contain V1 only. They include accepted
Findings and RevisionActions, but no actual revised manuscript. Acceptance of a
RevisionAction is not treated as permission for the system to rewrite the
author's prose.

## Current decision boundary

Gate B therefore remains pending. A qualifying next step is either:

1. the author supplies two substantive V2 manuscripts; or
2. the author explicitly authorizes creation of isolated candidate V2 drafts,
   then reviews and adopts or rejects those drafts before they are counted as
   author versions.

Candidate drafts must never overwrite or publish the blog source. A
model-generated draft that has not been reviewed by the author is test input,
not Gate B evidence.

## Candidate-draft checkpoint

On 2026-08-23 the author explicitly authorized isolated candidate drafts for
`the-structural-stand-in.md` and
`the-dynamic-dialectic-of-knowledge-systems-on-change-and-invariance-in-theoretical-identity.md`.
The candidates were generated outside both repositories and accompanied by a
RevisionAction coverage audit. They remain outside the Gate corpus until the
author adopts them. This checkpoint records authorization and lifecycle only;
it deliberately does not publish manuscript text or local private paths.

Both final candidate byte streams were also exercised in disposable copies of
the V1 workspaces. Each imported as an immutable V2, accepted a valid Raw IR,
deterministically generated a Reviewed IR and structural diff, and passed
`ir verify-project`. The preflight Reviewed IRs contain 15 Claims each. This
proves that the candidates are technically importable; it does not turn a model
draft into an author version. The preflight copies are excluded from the Gate B
corpus unless and until the author explicitly adopts the corresponding text.
