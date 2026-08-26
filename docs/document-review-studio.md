# Document Review Studio

> Status: **experimental preview**. Deterministic local Findings have a stable
> check-ID revision/recheck loop. External-model Findings have a critic-bound
> recheck request/import and human Resolution loop. This is still not formal
> V1: Gate C evidence and the product limitations below remain open.

Document Review Studio is the document-first workflow in this repository. It
keeps the uploaded bytes separate from the internal review representation and
does not require a model SDK or cloud account.

## Start the local UI

```powershell
python critic_runner.py studio
```

The server binds to loopback only. Use `--no-browser` to print the local URL
without opening a browser, `--data-dir` to choose the local project library,
or `--project` to open an existing `.document-review-studio` project.

The project library page has an explicit, irreversible delete action. It only
accepts a project directory directly inside the configured library; deleting a
project removes its local source, audit artifacts, receipts, and exports. Copy
the project directory elsewhere first if it is needed as a backup.

Inside a project, the header shows the seven-stage workflow and one suggested
next action. The extraction preview can search long documents by text or block
identifier. Finding review is filtered by critic, severity, and decision state.
Findings with the same stable location and normalized modification action are
displayed as one attention work group while every critic's evidence, reason,
and decision remains atomic. The first view contains at most 30 work groups;
the complete queue requires explicit expansion.

The UI flow is intentionally gated:

1. Upload `.md`, `.txt`, `.docx`, or `.pdf`.
2. Inspect extraction quality and warnings.
3. Confirm, correct, continue with warning, or replace the upload.
4. Confirm document type, jurisdiction, effective date, publisher, audience,
   publication status, and risk-domain context.
5. Run the clearly labelled deterministic local precheck, or export five
   critic-specific AI protocols and import each raw JSON response.
6. Decide each Finding manually.
7. Generate work-group/Finding-set Actions from accepted Finding decisions.
   The system suggests block replacement, before/after insertion, block
   deletion, table-cell replacement, or section append, but a human must
   explicitly select and justify the operation before any Hunk is accepted.
8. Enter an exact Hunk (human-authored or manually imported from AI), inspect
   the operation and before/after text, and approve or reject it.
9. Materialize only approved Hunks into a new document version and re-run every
   original deterministic local critic. Aggregate deterministic checks compare
   stable check data so a partial fix remains `partially-resolved`, and newly
   introduced Findings are added to the risk report.
10. For every external-model critic, export the request bound to the complete
    original prompt snapshot, request, AuditRun, critic protocol,
    provider/model, current Revision, and revised-text hash. Import the new
    provider/model declaration and response, then confirm each original
    Finding Resolution manually. New Findings cannot receive a Resolution;
    they and human-confirmed partial/unresolved items become open Findings in
    a follow-up round based on the revised document.
11. Export revised Markdown/DOCX, the difference report, unresolved-risk
    report, recheck result, and complete audit package.

## Storage and safety

Each project is a `.document-review-studio` directory containing:

- `source/<original-name>`: exact uploaded bytes, never overwritten;
- `project.json`: source type, byte count, SHA-256, and parser-independent
  identity;
- `extraction/document.json`: the format-neutral block model;
- `extraction/quality.json`, `warnings.json`, and `source-map.json`;
- `extraction-decisions/`: append-only human extraction confirmations,
  corrections, warning continuations, and replacement requests;
- `context.json`: human-confirmed review context;
- `ai-requests/<request>/`: provider/model-bound critic prompts;
- `audits/<critic>/`: raw model responses and parsed immutable runs;
- `finding-decisions/`: append-only human decisions;
- `revision-plans/`: immutable Actions bound to the complete current Finding
  decision set, including accept/correct/reject/defer;
- `action-operation-decisions/`: append-only human choices that turn an
  operation suggestion into an authorized Action operation;
- `revision-hunks/`: append-only exact replacement proposals with stable
  before-text hashes;
- `hunk-decisions/`: immutable human approvals or rejections;
- `revisions/`: atomically staged materialized drafts, diffs, unresolved risks,
  local rechecks, external recheck requests/results, human Resolutions, and
  version-bound follow-up Finding rounds;
- `exports/`: audit reports, drafts, DOCX output, and the revision bridge;
- `integrity-index.json`: append-only project register of every tracked artifact,
  its receipt, content hash, sequence, and predecessor index head;
- `audit-log.jsonl`: a protected append-only event chain with event sequence and
  previous-event hash;
- `state.json`: a rebuildable UI/cache snapshot, never an authorization source.

Every protected artifact has a receipt binding its content SHA-256, parent
artifact hashes, provenance, and the immutable `integrity-policy.json` marker.
The append-only index is the expected artifact set, so deleting an artifact and
its receipt together cannot make the project silently fall back to an earlier
decision or an earlier audit state. The chain covers the source, structured
extraction/corrections, context, critic prompt, raw response, parsed
run/Finding, sequenced human decision, revision Action/Hunk/decision,
materialized revision, recheck, revision bridge, and export. Missing
index entries, receipts, changed parents, broken decision sequences, and
deletion of the policy marker force read-only mode. This detects ordinary local
tampering; it is not a keyed signature against an attacker who can rewrite the
entire project and every receipt/index entry. In particular, the mechanism can
detect ordinary modification and deletion, but cannot resist an attacker who can
restore an older index, its receipt, and the corresponding project snapshot.
Strong rollback protection requires an external trusted checkpoint or signature.

The review gate derives extraction authorization from the latest
`extraction-decision` and its bindings to the current document, quality, and
warnings artifacts. It never trusts `state.json` for that decision. Context
confirmation is rejected until a valid extraction decision exists. The audit
log is part of the integrity chain; replacing or truncating it forces
read-only mode.

## Parser limits

- Markdown and TXT use the built-in UTF-8 parser.
- DOCX is inspected as a safe OOXML ZIP. Paragraph styles, lists, tables,
  merge metadata, headers/footers, hyperlinks, images, footnote/comment
  presence, and revision markers are recorded. Unaccepted revisions are a
  high-risk warning and cannot be silently omitted.
- PDF prefers `pypdf` and `pymupdf` when installed. A conservative built-in
  literal-text fallback handles simple text PDFs while explicitly reporting
  that coordinates, tables, and reading order are unavailable.
- Scanned pages use the replaceable `TesseractOCR` adapter. Install Tesseract
  5.x and `chi_sim`, `chi_tra`, and `eng` language data. Missing OCR or PDF
  rendering produces a visible diagnostic and prevents formal review.

Run `python critic_runner.py doctor` to see parser and OCR availability and the
declared license boundary for each optional component. The Studio environment
card can repair missing Python adapters in one action (currently `pypdf` and
`PyMuPDF`). After a successful repair, an open project that was blocked before
human extraction confirmation is automatically re-ingested; the same retry is
also available as an explicit “重新识别” action. Tesseract is an operating-system program and language-pack choice,
so it is reported with an installation hint rather than silently installed.
The same repair is available from the terminal with
`python critic_runner.py doctor --repair`.
The dependency ranges are in `pyproject.toml`; PyMuPDF's AGPL/commercial
choice must be reviewed for the deployment context.

## Review contract

The five independent dimensions are:

- `expression_ambiguity`;
- `execution_feasibility`;
- `compliance_legal_screen`;
- `reasonableness_governance`;
- `official_professional_format`.

Each Finding carries a stable block/page/cell location, evidence, issue,
standard, consequence, severity, verification state, external basis,
uncertainties, suggested action/owner, and release-blocking flag. A legal
screen without supplied sources can only create `cannot-confirm` follow-ups;
it cannot output an unconditional legal conclusion. Zero-Finding runs retain
their inspection scope and basis in the audit package.

The bundled keyword/structure rules are labelled as a deterministic local
precheck, not as professional AI review. Each AI critic has a distinct role,
objective, checks, evidence standard, and exclusions. The browser supports the
full export → external model → import path. The same path is available in the
CLI:

```powershell
python critic_runner.py studio-protocols <project> --provider <provider> --model <model>
python critic_runner.py studio-import-ai <project> <critic> <response.json> --provider <provider> --model <model> --request-id <request-id>
# If the model cannot echo the bookkeeping envelope:
python critic_runner.py studio-import-ai <project> <critic> <response.json> --provider <provider> --model <model> --request-id <request-id> --binding-mode manual_association
```

The exported prompt recommends a response envelope containing the exact
`request_id`, `prompt_sha256`, provider, and model. Strict imports require all
four values to echo the selected request. `prompt_sha256` identifies the
protocol payload; `prompt_file_sha256` separately binds the rendered prompt
file.

Some models cannot reliably echo these bookkeeping fields. The browser therefore
defaults to an explicit `manual_association` mode. It accepts an otherwise valid
ordinary JSON response without requiring the model to reproduce the source hash,
but records
`response_binding.mode=manual-association` and
`request_echo_verified=false`, `source_echo_verified=false`, and
`source_associated_by_application=true`. This means the user associated the response
with the selected request; it does not prove that the response was generated
for that prompt. A conflicting source hash is always rejected. A response
containing only some request-envelope fields is rejected in both modes to avoid
silently accepting a misleading partial binding.

The generated protocol includes an exact JSON response example, legal values for
`verification_state`, and the complete `external_basis` object shape. On import,
common unsupported verification labels are conservatively downgraded to
`needs-human-verification`, and a null/string/array `external_basis` becomes an
empty structured basis with an explicit unresolved-fact marker. Every such
normalization is stored in `response_normalizations`; the raw model response is
preserved unchanged. Missing substantive Finding fields, conflicting identities,
invalid locations, and mismatched hashes are never repaired automatically.

The current browser/CLI flow is a manual import, so parsed runs store
`declared_model_metadata` rather than claiming a direct `model_invocation`.
Imports preserve the declared metadata, raw response hash/content, and parsed
AuditRun/Finding separately. `collect_model_audit` validates the source hash,
critic identity, Finding contract, and block locations in either mode, and
validates the request envelope in strict mode.

The browser keeps exactly one active request per critic, preserves superseded
requests as protected history, shows a per-critic `imported/not imported` count,
provides copy, previous/next, and five-protocol ZIP controls, and shows every
generated export in an export center. Exported files can be downloaded
individually, and the containing folder can be opened from the local desktop.
Formal export requires confirmed extraction/context, at least one current audit,
and a decision for every current Finding. A final revised-document export also
requires a current revision plan, one decided latest Hunk per Action, anchor
hash verification, and revision materialization. Rejected Hunks are never
applied and remain visible in `未解决风险.md`. The complete audit ZIP includes
the source and protected project history needed for local verification.

A revision plan and Revision bind the digest and individual hashes of **all**
current Finding decisions, not only accepted Findings. Changing reject to defer,
or making any other decision change, invalidates the old Revision and prevents
it from being exported alongside the new audit state. Local recheck uses stable
`check_id`/`check_data`, with explicit `resolved`, `partially-resolved`,
`still-present`, and `new-finding` states; natural-language issue equality is
never used as the resolution key. Recheck/report generation occurs before a
private staging directory is atomically promoted, so a failed recheck can be
retried without a half-created Revision directory.

Before the constrained revision chain is complete, DOCX preview exports are
named `normalized-editable-copy.docx`; they may lose source layout and are not a
revision. After every Action has a decided latest Hunk and the revision is
materialized, the export contains `修改稿.docx`, `修改稿.md`, `修改说明.md`,
`未解决风险.md`, and `recheck.json`. The DOCX is generated from the approved
internal text model and does not claim to preserve source layout or Word Track
Changes. A human `corrected_action` supersedes the critic's original
instruction, but cannot change text until an exact Hunk is separately approved.
`correct` requires a non-empty bounded corrected action; `accept` alone may
inherit the critic's suggested action.

## Current product limitations

- Output DOCX does not use native Word Track Changes and does not preserve the
  original DOCX styles, headers/footers, images, or exact table layout in place.
- Table-container, multi-paragraph, and cross-block structural edits still need
  a richer editor. Table/page-break container anchors are refused rather than
  silently relocated; there is not yet a Finding-location correction UI.
- The Studio does not yet generate a constrained AI drafting protocol for each
  revision Action. Users enter or manually import the exact Hunk text.
- Initial critic protocol export/import has CLI commands, but the full
  Action/Hunk/external-Resolution revision workflow is browser-only.
- External Resolution is only complete after the bound response is imported
  and a human confirms every original item. New items are never labelled
  resolved at discovery time: they remain `new-finding-awaiting-next-round`
  until promoted into the next revision round's normal Finding queue.
- If every Hunk is rejected, the current package still uses the “修改稿” output
  name even though its risk report records that no proposed modification was
  applied. A distinct no-change completion package remains to be implemented.
