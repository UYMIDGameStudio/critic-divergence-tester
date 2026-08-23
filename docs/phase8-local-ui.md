# Phase 8 local Argument Workbench UI

Phase 8 adds a local application layer without making the core Python engine
depend on a GUI framework. The command is:

```powershell
py -3 critic_runner.py ir ui .\draft.argument-workbench
```

It chooses a free port, listens only on `127.0.0.1`, opens the default browser,
and stops on Ctrl+C. `--no-browser` prints the URL for headless or remote test
environments. Non-loopback hosts are rejected rather than turning an
unpublished manuscript into a network service.

## Document-first workspace

The default view is three linked panes:

1. **Manuscript** shows immutable source lines and deterministic Claim
   locations. Clicking a Claim marker selects that Claim.
2. **Argument** shows the selected Claim, type, method and role, then its real
   incoming and outgoing relations. It does not coerce the graph into a tree.
3. **Review** shows every Rule and Perspective Lens outcome for the Claim,
   preserving PASS/FAIL/UNCERTAIN disagreements. It also shows the current
   human decision, RevisionActions, Citation verification state and Claim
   Lineage.

The header is a project dashboard: current draft, Claim count, open/deferred/
accepted Findings, human-confirmed resolved Findings and unverified Citations.
There is no manuscript score, Lens vote or automatic synthesis.

Version selection is read-only for earlier drafts. The current draft permits
`accept`, `reject` and `defer`. Accept requires an action type and free-text
revision action. The browser delegates this operation to the existing
`append_finding_decision` service, producing the same immutable
`finding-adjudication` and `revision-action` artifacts as the line-oriented
workflow. The UI has no private alternative database.

## Local security boundary

The HTML shell contains no manuscript content. Artifact data is returned only
from `/api/view` when the caller supplies a random per-process token embedded
in that shell. Mutating requests require the same token, strict JSON content,
an exact field set, and a one-megabyte body limit. Responses disable caching,
framing, MIME guessing and cross-origin content through security headers.

This is a local privacy boundary, not a multi-user authentication system. The
process should not be exposed through a reverse proxy. Model prompts still
leave the machine only when the user sends them to a chosen model provider.

## Verified vertical slice

The public Chinese Workbench fixture was opened in a real browser. The page
showed three Claims, two open Findings and one unverified Citation. Selecting
`C2` moved the manuscript highlight and displayed `E1 -> C2`, `Z1 -> C2`,
`C2 -> C1`, four Social Science PASS outcomes and the unverified Girard
Citation. No decision was written during visual QA.

Automated tests additionally prove that:

- the projection refuses an invalid project before serving it;
- model outcomes and human decisions remain separately labelled;
- a browser adjudication uses the existing append-only contract;
- the HTTP API rejects requests without the unpredictable local token;
- non-loopback listeners are refused; and
- the application shell contains no quality score.

## Current limitations

- One running UI process opens one Project / D1 workspace. A multi-project
  library dashboard is not yet implemented.
- Raw model collection, IR correction, Lens preparation/result collection,
  lineage adjudication, resolution confirmation and Citation confirmation
  retain their existing line-oriented interactions. They do not require hand
  editing JSON, but Phase 8 does not duplicate every command as a web form.
- The source pane highlights Claim-bearing lines rather than rendering nested
  overlapping ranges. Exact source quotes and canonical positions remain
  visible in the Argument pane.
- Full graph visualization is intentionally auxiliary; the current UI uses the
  relation list because the product is document-first.
- The server has no remote collaboration, account, sync or cloud storage.

These limits are application UX limits, not gaps in artifact provenance.
