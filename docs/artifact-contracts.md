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
| `rule-review-run` | immutable | One Reviewed IR snapshot, Rule Lens library, deterministic check plan, and execution prompt |
| `review-result-attempt` | immutable | One exact model response to a Rule Review plan and its reproducible validation outcome |
| `claim-review-index` | derived-replaceable | Claim-grouped PASS/FAIL/UNCERTAIN outcomes plus exact links to actionable Finding artifacts |
| `argument-finding` | immutable | One lens/check verdict attached to a version-qualified Claim; initial status is only `open` |
| `finding-adjudication` | immutable | Human `accept`, `reject`, or `defer`; later changes use `supersedes` |
| `revision-action` | immutable | Structured action attached to an accepted adjudication |
| `revision-plan-record` | derived-replaceable | Reproducible Markdown plan and current decision/action projection |
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

## Rule Review provenance

`rule-review-run` is self-contained. It snapshots the exact Reviewed IR record, compatible Argument IR v1 payload, and check-library bytes used to generate its plan. The run binds all three as parents and records exact hashes for the plan and prompt. Later IR corrections create a new review run rather than mutating or disconnecting the old one.

Every collected model response is an immutable `review-result-attempt`, including invalid JSON and results that fail the existing plan-bound validator. Only a valid attempt can produce derived review artifacts. The original response remains the semantic source; no application code silently changes verdicts, reasons, or evidence references.

For valid results, each FAIL or UNCERTAIN outcome becomes an immutable `argument-finding` with a version-qualified `target_claim`, Rule Lens/check identity, `status=open`, and exact parents for the target IR and model result. PASS remains visible in `claim-review-index` and `claim-review.md` but does not create an actionable Finding. The deterministic index groups outcomes by Claim and binds every Finding hash. Its required `field_provenance` map keeps verdict, reason, consequence, and evidence references explicitly `model-derived`, while task/Claim/check mapping, Finding IDs, counts, and view rendering are deterministic. Deterministic packaging never turns model judgments into deterministic facts.

## Human adjudication and revision-plan provenance

Workbench decisions reuse the legacy workflow's `accept` / `reject` / `defer` field rules through one adapter, while remaining separate Claim-centered artifacts. Accept requires at least one structured RevisionAction. Reject and defer require a human reason. The model result and immutable Finding remain untouched in every case.

Each `finding-adjudication` binds the exact Finding bytes. Reconsidering a Finding appends another adjudication with `supersedes` and the previous adjudication hash; it never edits or deletes the earlier decision. A `revision-action` binds the exact accepted adjudication and uses one of the documented action types plus unconstrained human text. Bundle validation rejects an accepted adjudication without at least one linked action.

`revision-plan/record.json` and `revision-plan.md` are deterministic caches. The record binds every current Finding, latest adjudication, and applicable action by exact-byte SHA-256, records the Markdown payload hash, and separates model-derived Finding fields from human-confirmed decisions and actions. Rebuild replaces only these derived files. Open, deferred, rejected, and accepted counts are workflow state, not a manuscript quality score.

## Legacy workflow boundary

`critic-adjudication` v1 remains the compatible report-oriented workflow. It is not silently treated as a Claim-centered Workbench adjudication. The Workbench shares its decision-field rules through an adapter, while `argument-finding`, `finding-adjudication`, and `revision-action` provide the product contract. A later migration can translate immutable legacy report evidence without changing either existing archive.

Campaign, divergence, W/B, blind scorecards, and generic-critic controls remain Evaluation/Advanced artifacts. Their scores are not manuscript quality measures and do not appear in the Workbench project view.
