# Argument Workbench artifact contracts

Phase 0 introduces a product lifecycle around the existing Argument IR v1. It does not replace the runner, report, campaign, or evaluation contracts.

## Common envelope

Every Workbench JSON artifact has `schema_version`, `artifact`, `artifact_id`, `lifecycle`, `provenance`, and `parents`. Validators reject unknown fields, malformed shapes, duplicate JSON keys at ingestion, and broken exact-byte SHA-256 links.

`provenance.origin` has only three meanings:

- `deterministic`: computed from bound bytes or deterministic replay. It does not mean the underlying semantic claim is true.
- `model-derived`: selected, classified, or proposed by a model.
- `human-confirmed`: explicitly entered or corrected by the user.

Lifecycle values are:

- `immutable`: never replaced; a later judgment is a new artifact.
- `append-only`: correction history only grows through new events.
- `derived-replaceable`: a cache that may be atomically replaced only by reproducing it from immutable parents.

Parent hashes always cover the exact file bytes on disk. Artifact objects do not contain a self-hash. Workbench-owned parents must be present in a validated Workbench bundle; existing external contracts such as `argument-ir` and `argument-check-results` remain under their existing validators and are rechecked by the application command that consumes the bundle.

## Artifact boundaries

| Artifact | Lifecycle | Canonical responsibility |
| --- | --- | --- |
| `argument-project` | immutable | Local project identity and human title |
| `argument-document` | immutable | Document identity inside a project |
| `document-version` | immutable | Version-local source name, bytes hash, and optional parent version |
| `raw-ir-attempt` | immutable | One exact model response, collection method, prompt hash, and validation outcome |
| `ir-correction` | append-only | One human correction or explicit revert event |
| `reviewed-argument-ir` | derived-replaceable | Binding from Raw IR and ordered corrections to a compatible Argument IR v1 payload |
| `argument-finding` | immutable | One lens/check verdict attached to a version-qualified Claim; initial status is only `open` |
| `finding-adjudication` | immutable | Human `accept`, `reject`, or `defer`; later changes use `supersedes` |
| `revision-action` | immutable | Structured action attached to an accepted adjudication |
| `claim-lineage` | immutable | Proposed or human-confirmed many-to-many correspondence between version-qualified Claims |

An accepted adjudication is incomplete unless the same validated bundle contains at least one RevisionAction linked to its exact hash. ClaimLineage uses arrays on both sides so split and merge are native rather than encoded as fake one-to-one identities. A model proposal is an immutable `status=proposed` artifact; a human confirmation or rejection is a second artifact that binds the exact proposal hash. Human-originated lineage may be confirmed directly but is still marked `proposed_by=human`.

## Field provenance in Reviewed IR

`reviewed-ir/argument-ir.json` deliberately remains Argument IR schema v1 so the existing `ir validate` and `ir plan` commands keep working. Its companion `reviewed-ir/record.json` is the formal Reviewed IR artifact and contains:

- exact hashes for DocumentVersion, Raw attempt, every correction, and the v1 payload;
- stable-reference to display-ID mapping;
- per-field provenance entries;
- correction hashes in replay order.

Raw fields begin as `model-derived`. Corrected or added fields cite the `ICnnnn` event that made them `human-confirmed`. Source name/hash, node positions, and compact display IDs are `deterministic`. Exact-quote validation proves only that a quote occurs at the stated source location; choosing that quote remains a model or human semantic act.

Deleting a node deterministically removes its incident relations from the reviewed projection. The removal itself remains traceable to its correction event. Undo appends `revert_correction`; it never deletes history.

## Legacy workflow boundary

`critic-adjudication` v1 remains the compatible report-oriented workflow. It is not silently treated as a Claim-centered Workbench adjudication. Phase 3 must adapt its immutable report evidence into `argument-finding` plus `finding-adjudication` rather than create a second product decision system.

Campaign, divergence, W/B, blind scorecards, and generic-critic controls remain Evaluation/Advanced artifacts. Their scores are not manuscript quality measures and do not appear in the Workbench project view.
