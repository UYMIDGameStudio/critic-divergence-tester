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
- `append-only`: correction or human-triage history only grows through new events.
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
| `review-status-triage` | append-only | One human acknowledgement or rejection of a model-proposed non-evaluated status, with an explicit follow-up action |
| `review-status-triage-index` | derived-replaceable | Reproducible open/acknowledged/rejected execution-status queue binding every triage event |
| `direct-review-baseline` | immutable | Exact direct-chat prompt/response, manuscript binding, provider/model identity, declared session conditions, and elapsed-time evidence for Gate A comparison |
| `gate-a-session-start` | immutable | Human invocation of one timed Gate activity, with start time captured from the system clock |
| `gate-a-work-session` | immutable | Completion of the exact start artifact with deterministic elapsed milliseconds; never edits the start |
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

`rule-review-run` is self-contained. It snapshots the exact Reviewed IR record, compatible Argument IR v1 payload, and check-library bytes used to generate its plan. Plan v2+ records a review scope independently of check depth: `thesis-chain` deterministically selects conclusion/intermediate Claims and their upstream support chain, `claim`/`claims` target explicit IDs, and `all` is a full audit. The run binds its inputs as parents and records exact hashes for the plan and prompt.

Every collected model response is an immutable `review-result-attempt`, including invalid JSON and results that fail the existing plan-bound validator. Only a valid attempt can produce derived review artifacts. The original response remains the semantic source; no application code silently changes verdicts, reasons, or evidence references.

Result v2 first records `execution_status`. Only `evaluated` tasks may carry `pass`, `fail`, or substantive `uncertain`; `blocked_missing_context`, `routing_mismatch`, and `not_applicable` require a reason and basis but no verdict. Result v3 additionally requires one `support_paths` entry for every PASS `support_ref`. Its relation IDs must form a directed path from that node to the target Claim and may use only `supports`, `qualifies`, and `cites`; `contradicts` and `assumes` are context, not PASS support. A citation-required path starts from a Citation through `cites`. Legacy v1/v2 plans and results remain byte-verifiable but do not gain stronger semantics retroactively.

Non-evaluated statuses never become manuscript Findings, but they no longer disappear from human workflow. Each current status remains open until a `review-status-triage` event acknowledges or rejects it. Routing mismatches point to IR correction/rerun actions, missing context points to context/evidence actions, and not-applicable requires explicit acknowledgement or rejection. Reconsideration appends another event binding the prior event; the derived triage index binds the entire exact-byte history. Open triage blocks Gate capture.

For valid evaluated results, each FAIL or substantive UNCERTAIN becomes an immutable `argument-finding` with a version-qualified `target_claim`, Rule Lens/check identity, `status=open`, and exact parents for the target IR and model result. PASS and non-evaluated statuses remain visible without creating actionable Findings. Deterministic packaging never turns model judgments into deterministic facts.

## Human adjudication and revision-plan provenance

Workbench decisions reuse the legacy workflow's `accept` / `reject` / `defer` field rules through one adapter, while remaining separate Claim-centered artifacts. Accept requires at least one structured RevisionAction. Reject and defer require a human reason. The model result and immutable Finding remain untouched in every case.

Each `finding-adjudication` binds the exact Finding bytes. Reconsidering a Finding appends another adjudication with `supersedes` and the previous adjudication hash; it never edits or deletes the earlier decision. A `revision-action` binds the exact accepted adjudication and uses one of the documented action types plus unconstrained human text. Bundle validation rejects an accepted adjudication without at least one linked action.

`revision-plan/record.json` and `revision-plan.md` are deterministic caches. The record binds every current Finding, latest adjudication, and applicable action by exact-byte SHA-256, records the Markdown payload hash, and separates model-derived Finding fields from human-confirmed decisions and actions. Rebuild replaces only these derived files. Open, deferred, rejected, and accepted counts are workflow state, not a manuscript quality score.

## Product Gate A evidence

Gate A is an Evaluation/Advanced lifecycle that blocks Phase 4 until real usage evidence exists. `ir gate-a prepare-baseline` deterministically creates the versioned `direct-full-manuscript-review-v1` prompt and embeds the bound source bytes verbatim. Before comparison, each project collects a `direct-review-baseline`: the exact full-text chat prompt and raw response bytes, human-supplied provider/model identity, source binding, start/completion timestamps, manuscript delivery mode, and conversation conditions. Inline collection and verification prove byte inclusion; attachment delivery remains an explicit human declaration. In controlled v2 artifacts the supplied timestamps remain `human-confirmed`, while elapsed milliseconds are their deterministic difference. New Gate capture requires a fresh session, no prior conversational context, and human confirmation that the model received the complete manuscript. It also rejects a baseline completed after the first valid Workbench Rule Review result. Historical v1 baseline bytes remain valid but are insufficient for a new controlled corpus. The artifact makes no clearer/same/worse judgment.

Human work timing is a separate append-only stream. `gate-a-session-start` captures the selected activity and system time immediately; `gate-a-work-session` later binds the exact start bytes and computes elapsed milliseconds from the two system timestamps. Neither artifact claims that the work was good, only that a human explicitly opened and completed a named activity interval. Open and completed sessions are verified as part of the Workbench.

A v5 corpus binds 3–5 distinct source hashes and the exact Project, DocumentVersion, Reviewed IR, Revision Plan, controlled direct baseline, every current status-triage index, and every eligible pre-review IR-inspection session for each local workspace. It refuses an open work session or a project without an IR-inspection session completed before the first Rule Review result. It stores local locators and hashes but never copies manuscript bytes into the Gate directory. Legacy v1–v4 Gate evidence remains verifiable under the contract in force when it was captured.

Each v5 assessment is `human-confirmed` and binds its corpus, Project, Revision Plan, controlled direct baseline, status-triage indexes, and the exact IR-inspection session records. Human comparison, correction-burden judgment, extraction-error counts, regression anchors, revision notes, and free text remain human-confirmed. `ir_inspection_timing` is separately marked deterministic and is the sum of the bound session milliseconds; v5 does not accept a self-reported `correction_minutes`. Historical v1–v4 assessments retain that legacy field.

The report is deterministic and replaceable. It exposes workflow completeness, open/accepted/rejected/deferred Finding counts, correction events, extraction traps, regression anchors, and human cost without reducing them to a score. The application refuses a human `pass` decision until all 3–5 bound workflows remain valid, no Finding is open, and every project has an assessment. Even then the program only establishes readiness: the gate decision and reason must be entered by a human. Later decisions append a new artifact with `supersedes`.

## Legacy workflow boundary

`critic-adjudication` v1 remains the compatible report-oriented workflow. It is not silently treated as a Claim-centered Workbench adjudication. The Workbench shares its decision-field rules through an adapter, while `argument-finding`, `finding-adjudication`, and `revision-action` provide the product contract. A later migration can translate immutable legacy report evidence without changing either existing archive.

Campaign, divergence, W/B, blind scorecards, and generic-critic controls remain Evaluation/Advanced artifacts. Their scores are not manuscript quality measures and do not appear in the Workbench project view.
