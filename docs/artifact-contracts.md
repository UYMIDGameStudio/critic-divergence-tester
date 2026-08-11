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
| `claim-review-index` | derived-replaceable | Claim-grouped substantive verdicts and auditable execution/routing states plus exact links to actionable Findings |
| `direct-review-baseline` | immutable | Exact direct-chat prompt/response, manuscript binding, model label, and elapsed-time evidence for Gate A comparison |
| `argument-finding` | immutable | One lens/check verdict attached to a version-qualified Claim; initial status is only `open` |
| `finding-adjudication` | immutable | Human `accept`, `reject`, or `defer`; later changes use `supersedes` |
| `revision-action` | immutable | Structured action attached to an accepted adjudication |
| `revision-plan-record` | derived-replaceable | Reproducible Markdown plan and current decision/action projection |
| `product-gate-a-corpus` | immutable | Human-selected private references and exact bindings for 3–5 real Workbench projects |
| `product-gate-a-assessment` | immutable | Per-manuscript human usability, extraction-quality, correction-cost, and regression-anchor observations |
| `product-gate-a-decision` | immutable | Human pass/fail/defer gate decision; reconsideration uses `supersedes` |
| `product-gate-a-report` | derived-replaceable | Reproducible gate evidence counts and Markdown, never an automatic score or decision |
| `claim-lineage` | immutable | Proposed or human-confirmed many-to-many correspondence between version-qualified Claims |

An accepted adjudication is incomplete unless the same validated bundle contains at least one RevisionAction linked to its exact hash. ClaimLineage uses arrays on both sides so split and merge are native rather than encoded as fake one-to-one identities. A model proposal is an immutable `status=proposed` artifact; a human confirmation or rejection is a second artifact that binds the exact proposal hash. Human-originated lineage may be confirmed directly but is still marked `proposed_by=human`.

## Field provenance in Reviewed IR

`reviewed-ir/argument-ir.json` deliberately remains Argument IR schema v1 so the existing `ir validate` and `ir plan` commands keep working. Its companion `reviewed-ir/record.json` is the formal Reviewed IR artifact and contains:

- exact hashes for DocumentVersion, Raw attempt, every correction, and the v1 payload;
- stable-reference to display-ID mapping;
- per-field provenance entries;
- correction hashes in replay order.

Raw fields begin as `model-derived`. Corrected or added fields cite the `ICnnnn` event that made them `human-confirmed`. Source name/hash, node positions, and compact display IDs are `deterministic`. Exact-quote validation proves only that a quote occurs at the stated source location; choosing that quote remains a model or human semantic act.

`extraction-prompt.md` is deterministic source-bound protocol content rather than a semantic judgment artifact. New workspaces use `argument-ir-extraction-v2`; verification also reproduces the exact legacy v1 bytes so existing immutable workspaces remain valid. Every Raw attempt binds the prompt bytes it actually used through `prompt_sha256`. Once an attempt exists, replacing v1 with v2 (or making any other prompt edit) therefore breaks verification instead of silently changing model provenance.

Deleting a node deterministically removes its incident relations from the reviewed projection. The removal itself remains traceable to its correction event. Undo appends `revert_correction`; it never deletes history.

## Rule Review provenance

`rule-review-run` is self-contained. It snapshots the exact Reviewed IR record, compatible Argument IR v1 payload, and check-library bytes used to generate its plan. Plan v2 records a review scope independently of check depth: `thesis-chain` deterministically selects conclusion/intermediate Claims and their upstream support chain, `claim`/`claims` target explicit IDs, and `all` is a full audit. The run binds its inputs as parents and records exact hashes for the plan and prompt.

Every collected model response is an immutable `review-result-attempt`, including invalid JSON and results that fail the existing plan-bound validator. Only a valid attempt can produce derived review artifacts. The original response remains the semantic source; no application code silently changes verdicts, reasons, or evidence references.

Result v2 first records `execution_status`. Only `evaluated` tasks may carry `pass`, `fail`, or substantive `uncertain`; `blocked_missing_context`, `routing_mismatch`, and `not_applicable` require a reason and basis but no verdict. They remain visible in the index and do not become revision Findings. `basis_refs` records what the model inspected. `support_refs` has the narrower meaning of evidence supporting PASS: `upstream-required` rejects a target Claim citing only itself, while `citation-required` requires an upstream Citation. Legacy v1 plans/results remain byte-verifiable but do not gain these stronger semantics retroactively.

For valid evaluated results, each FAIL or substantive UNCERTAIN becomes an immutable `argument-finding` with a version-qualified `target_claim`, Rule Lens/check identity, `status=open`, and exact parents for the target IR and model result. PASS and non-evaluated statuses remain visible without creating actionable Findings. Deterministic packaging never turns model judgments into deterministic facts.

## Human adjudication and revision-plan provenance

Workbench decisions reuse the legacy workflow's `accept` / `reject` / `defer` field rules through one adapter, while remaining separate Claim-centered artifacts. Accept requires at least one structured RevisionAction. Reject and defer require a human reason. The model result and immutable Finding remain untouched in every case.

Each `finding-adjudication` binds the exact Finding bytes. Reconsidering a Finding appends another adjudication with `supersedes` and the previous adjudication hash; it never edits or deletes the earlier decision. A `revision-action` binds the exact accepted adjudication and uses one of the documented action types plus unconstrained human text. Bundle validation rejects an accepted adjudication without at least one linked action.

`revision-plan/record.json` and `revision-plan.md` are deterministic caches. The record binds every current Finding, latest adjudication, and applicable action by exact-byte SHA-256, records the Markdown payload hash, and separates model-derived Finding fields from human-confirmed decisions and actions. Rebuild replaces only these derived files. Open, deferred, rejected, and accepted counts are workflow state, not a manuscript quality score.

## Product Gate A evidence

Gate A is an Evaluation/Advanced lifecycle that blocks Phase 4 until real usage evidence exists. Before comparison, each project collects a `direct-review-baseline`: the exact full-text chat prompt and raw response bytes, human-supplied model label, source binding, and start/completion timestamps. Elapsed milliseconds are deterministic. Gate capture rejects a baseline completed after the first valid Workbench Rule Review result, because it would no longer be an uncontaminated comparison. The artifact makes no clearer/same/worse judgment.

A v2 corpus binds 3–5 distinct source hashes and the exact Project, DocumentVersion, Reviewed IR, Revision Plan, and direct baseline bytes for each local workspace. It stores local locators and hashes but never copies manuscript bytes into the Gate directory. Legacy v1 Gate evidence remains verifiable.

Each v2 assessment is `human-confirmed` and binds its corpus, Project, Revision Plan, and direct baseline. It records the human comparison, correction burden and minutes, extraction-error counts, at least one regression anchor, optional actual-revision notes, and free text. These observations are not inferred automatically.

The report is deterministic and replaceable. It exposes workflow completeness, open/accepted/rejected/deferred Finding counts, correction events, extraction traps, regression anchors, and human cost without reducing them to a score. The application refuses a human `pass` decision until all 3–5 bound workflows remain valid, no Finding is open, and every project has an assessment. Even then the program only establishes readiness: the gate decision and reason must be entered by a human. Later decisions append a new artifact with `supersedes`.

## Legacy workflow boundary

`critic-adjudication` v1 remains the compatible report-oriented workflow. It is not silently treated as a Claim-centered Workbench adjudication. The Workbench shares its decision-field rules through an adapter, while `argument-finding`, `finding-adjudication`, and `revision-action` provide the product contract. A later migration can translate immutable legacy report evidence without changing either existing archive.

Campaign, divergence, W/B, blind scorecards, and generic-critic controls remain Evaluation/Advanced artifacts. Their scores are not manuscript quality measures and do not appear in the Workbench project view.
