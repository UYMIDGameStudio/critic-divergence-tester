# Product Definition of Done audit

This audit closes roadmap item 24 at the current local-first, provider-neutral
product boundary. It does not claim that extraction, criticism, lineage or
Citation judgments are true. It establishes that every required human action
has a non-JSON interaction and that its result remains traceable.

## End-to-end acceptance matrix

| User outcome | Product surface | Evidence |
| --- | --- | --- |
| Import a manuscript | `ir init` | Creates Project, Document, immutable V1 source and a source-bound extraction prompt. |
| Ask any model to extract Argument IR | generated `extraction-prompt.md` + `ir collect` | Every response, including invalid output, receives a new immutable attempt. |
| See Claims in the source | `ir ui` Manuscript pane | Canonical positions are deterministic; selecting a Claim links all three panes. |
| Correct extraction without editing JSON | `ir inspect` | Line-oriented add/update/remove/relation/binding operations append `ir-correction`; undo appends a revert event. |
| Understand Claims, Evidence, Assumptions and Citations | UI Argument pane + `argument-map.md` | Incoming and outgoing graph relations remain a relation list, not a fake tree. |
| Select Review Lenses | `ir review prepare`, `prepare-perspective` | Rule scope/depth and Perspective framework are explicit and separate. |
| See which Claim each Finding attacks | UI Review pane | Outcomes and Findings use version-qualified target Claims. |
| Know the attack standard | expandable Lens basis in UI | Rule checks show the exact question, failure condition and evidence policy; Perspective outcomes show the complete preserved protocol. |
| Accept, reject or defer | UI or `ir adjudicate` | Both call the same append-only human adjudication service. |
| State what an accepted criticism requires | UI RevisionAction fields | Accept is invalid without at least one structured action plus free text. |
| Obtain a revision plan | `ir revision-plan --show` | JSON record and Markdown are deterministic caches bound to Findings, current decisions and actions. |
| Import the revised draft | `ir import-version` | Creates a new immutable source-bound DocumentVersion; local Claim IDs may change. |
| See exact structural changes | `ir diff-versions` | Text, node and relation equality changes are deterministic and kept separate from semantic correspondence. |
| Propose and confirm semantic change | `ir lineage prepare/collect/adjudicate` | Model proposals support split/merge/new/removed; human confirm/reject/correct is a separate artifact. |
| Inherit old Findings | `ir resolve prepare` | Requires an accepted Finding, its actions and confirmed descendant Lineage. |
| Re-run the original standard | `ir resolve collect` | Snapshots and reuses the original Rule library or complete Perspective protocol. |
| Confirm whether the issue was resolved | `ir resolve decide` | Resolved/partial/unresolved/obsolete/uncertain remains proposed until a human decides. |
| Verify evidence and Citations | `ir citations ...` + UI state | Four epistemic dimensions remain separate; unverified evidence never becomes `claim_false`. |
| View the whole Argument History | UI `Argument History` timeline | Shows every version, correction/Finding state, Lineage transition and Finding Resolution. |
| Trace any Finding | expandable provenance in UI | Shows exact hashes for source, Reviewed IR, Review run, Lens protocol, raw model result, Finding, human decision and RevisionActions. |
| Verify the complete local project | `ir verify-project` | Replays derived artifacts and rejects broken hashes, unsafe paths, stale caches and provenance gaps. |

## Product-language checks

Normal writing uses Projects, Documents, Versions, Claims, Review Lenses,
Findings, decisions, RevisionActions and Argument History. It does not require
the user to understand prompt engineering, Claude Code agents, I/C campaign
labels, W/B divergence calibration or timestamped run directories. Evaluation
and legacy report workflows remain available but are not the default entry.

The UI contains no Argument Score, manuscript quality percentage, Lens vote or
automatic synthesis. Deterministic, model-derived and human-confirmed origins
remain visibly distinct. Earlier versions are view-only and no Raw response,
correction, adjudication, Lineage decision, resolution decision or Citation
decision is silently overwritten.

## Real-use evidence

- Product Gate A was completed on three author-controlled manuscripts and has
  a human-confirmed `pass`; the correction burden and review-queue limitations
  remain recorded in [`product-gate-a.md`](product-gate-a.md).
- Product Gate B was completed on two author-controlled V1→V2 projects with 25
  human Lineage decisions and 23 human Finding Resolution decisions, including
  split/merge behavior; the author made the `pass` decision documented in
  [`product-gate-b.md`](product-gate-b.md).
- A real V2 Citation completed source lookup, model proposal, human
  confirmation and downstream dependency propagation as documented in
  [`phase7-citation-demo.md`](phase7-citation-demo.md).
- The local application was tested in a real browser on the public Chinese
  fixture. Claim selection, Rule basis, Citation state, provenance expansion
  and the whole-project Argument History dialog rendered without console
  errors. The UI details and limits are in
  [`phase8-local-ui.md`](phase8-local-ui.md).

## Retained limitations

- Model execution is deliberately manual/provider-neutral: the Workbench
  generates and collects exact prompts/results but does not prioritize a large
  set of provider SDKs.
- One UI process serves one Project/D1 workspace. There is no multi-project
  library, account, remote collaboration or cloud sync.
- IR correction, model collection, Lineage/Citation confirmation and Resolution
  confirmation remain line-oriented interactions. They do not require opening
  or editing JSON, but they are not all duplicated as browser forms.
- Source highlighting is line-level when exact Claim ranges overlap. The exact
  quote and canonical range remain available in the Argument pane.
- Gate pass decisions establish author acceptance of the workflow, not model
  accuracy, calibration or universal usability.

These limitations are explicit follow-on UX opportunities. None requires
pretending that a model judgment is deterministic or bypassing human judgment.
