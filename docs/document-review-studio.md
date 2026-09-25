# Document Review Studio

> 文书与学术已统一入口：运行 `python critic_runner.py app`，`studio` 为兼容别名。
> 本文主要记录原文书五维度和共用底层流程；学术三维度、综合八维度与兼容策略
> 见 [统一工作台说明](unified-workbench.md)。

> Version 0.2.3 — **experimental preview**. Deterministic local Findings have a stable
> check-ID revision/recheck loop. External-model Findings have a critic-bound
> recheck request/import and human Resolution loop. This is still not formal
> V1: Gate C evidence and the product limitations below remain open.

For targeted independent defense and evidence assessment of an imported finding,
see [Adversarial deep review](adversarial-review.md). The same workflow supports
academic papers and documents while preserving their different review standards.

Document Review Studio is the document-first workflow in this repository. It
keeps the uploaded bytes separate from the internal review representation and
does not require a model SDK or cloud account.

## Start the local UI

```powershell
python critic_runner.py app
```

The server binds to loopback only. Use `--no-browser` to print the local URL
without opening a browser, `--data-dir` to choose the local project library,
or `--project` to open an existing `.document-review-studio` project.

Each launch uses a new local port and token, so close pages left over from an
earlier run. The interface defaults to **繁體中文** and offers **English** in the
header. The selected language is saved locally and in the configured library,
including across a restart with a different port. Switching it changes interface
text, while original passages, filenames, evidence and your typed text stay intact.
Service diagnostics that may contain user content remain available in their
original wording under operation details. Research compatibility views are
documented separately in [the unified workbench guide](unified-workbench.md).

For actions with a request receipt, an interrupted response causes one retry
using the same request ID and payload. A committed result is reused, preventing
duplicate audit records. If the result remains uncertain, the page reads the
project state and asks you to check what completed. Actions without that receipt
contract are not blindly replayed. Reusing an ID with different content is refused.

The project library page has an explicit delete action with a verified pre-delete backup. It only
accepts a project directory directly inside the configured library; deleting a
project removes its working directory after a verified backup is written under
the library's `backups/` directory. Restore a backup into an independent project
from the home page or `studio-manage restore`; existing projects are never replaced.

Inside a project, the header shows the seven-stage workflow and one suggested
next action. Step buttons and **Continue / 繼續** navigate to the corresponding
controls, opening enclosing details when needed. The extraction preview can search long documents by text or block
identifier. Finding review is filtered by critic, severity, and decision state.
Findings with the same stable location and normalized modification action are
displayed as one attention work group while every critic's evidence, reason,
and decision remains atomic. The first view contains at most 30 work groups;
the complete queue requires explicit expansion. Once a local check produces no
Findings, the decision and modification steps are optional and **Export review
findings only** remains available. An empty Finding list is not a quality approval.

Editable form drafts are saved on this computer with a revision number and the
current document binding. AI responses are additionally bound to the selected
request; editing instructions are bound to their action/operation decision.
Changing project, task or effective document does not reuse another context's
draft. Refreshing restores saved fields and scroll position. Selected review
dimensions survive language changes and rerunning checks. Language changes before
upload also preserve the selected file, title and encoding; browser refresh before
upload still requires selecting the file again.

Use **Save draft / 儲存草稿** to retry a failed save, or download the current draft
before resolving a conflict. A stale page cannot overwrite a newer draft from
another page. Unsaved or pending changes trigger a browser leave warning.
During a write operation, including exit, the project form is temporarily inert.
Exit first waits for the draft to save, then shuts down the service and displays
the closed state; a failed save leaves the form available for recovery.

The UI flow is intentionally gated:

1. Upload a supported document and select its encoding or OCR language if needed;
   see the format table below. Browser uploads are limited to 30 MiB.
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
- `.ui-draft.json`: versioned, document-scoped editable fields; these do not
  authorize a review decision or modification;
- `.requests/`: committed action receipts used for safe request replay;
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

Multi-file writes use a recovery journal under the project lock. Before changing
existing files, the transaction records their original bytes. A failure rolls
back those writes and removes files created by that transaction; after a process
crash, opening the project recovers a validated pending journal before continuing.
Recovery validates the entire recorded restore set before applying it. Damaged
recovery data is reported and preserved for restoration from a trusted backup.
This handles interrupted local operations; it does not replace an independent
backup for disk failure or loss of the entire library.

## Formats, languages and extraction limits

The text layer supports English, Simplified Chinese, Traditional Chinese, German,
French, Japanese, Russian and Latin, including mixed Unicode documents. Accents,
ligatures, macrons, kana and Cyrillic characters are preserved when decoded
correctly. **Text parsing is not evidence of model review quality in all eight
languages.** Local keyword rules have limited language coverage; an external
model's reasoning and sources still require human review in the document's language.

| Format | Extracted content and important limits |
| --- | --- |
| Markdown/TXT (`.md`, `.markdown`, `.txt`, `.text`, `.log`) | Text blocks and headings with source-line locations; supported text encodings can be selected explicitly. |
| CSV/TSV | Quoted delimiters and multiline cells are parsed as cells with stable row/column locations. These are stored text values; formulas are not executed. |
| HTML/HTM | Static headings, paragraphs, lists and tables. Scripts, styles, templates and recognized hidden content are omitted. No scripts run and no external resources are fetched; this is not a browser rendering or a full CSS layout analysis. |
| RTF | Text, Unicode escapes, code pages and basic paragraph structure. Embedded images/objects and unsupported destinations are omitted with warnings; complex tables/layout are not preserved. |
| Word (`.docx`, `.docm`) | OOXML paragraphs, lists, tables and structural metadata. Macros are not run; images, footnotes, comments and unaccepted revisions have explicit extraction limits/warnings. |
| ODT | Paragraphs, lists and tables from the document package; the output is a normalized review representation. |
| Excel (`.xlsx`, `.xlsm`) | Worksheets and cell locations, including marked hidden sheets. Formula values come from the file's saved cache and may be stale; missing caches are warned about. No formulas, macros or external workbook links are executed. |
| PowerPoint (`.pptx`, `.pptm`) | Slide text, tables and speaker notes in presentation order, with hidden-slide markers. Animations, chart rendering and visual layout are not reproduced. |
| Old Office/WPS (`.doc`, `.wps`, `.xls`, `.et`, `.ppt`, `.dps`) | Recognizable RTF/HTML/OOXML content is parsed directly; real legacy binary files require local LibreOffice conversion. Missing or failed conversion is reported. Binary DOC/XLS/PPT samples passed local 0.2.2 acceptance; native WPS specimens remain unverified. |
| Text PDF | Prefers optional `pypdf`/`PyMuPDF`. The conservative built-in fallback only handles simple literal text and explicitly lacks coordinates, table reconstruction and reliable reading order. |
| Scanned PDF | Requires a PDF rendering component, Tesseract and each selected OCR language pack. Recognition quality must be checked before review. |

All formats retain the original uploaded bytes. A text extraction does not claim
to preserve the original layout, every embedded object or the behavior of an
Office document. Apple Pages is not currently supported. Size, nesting and
structural limits reject oversized or malformed input instead of claiming a
complete extraction of silently truncated content.

### Choosing text encoding

Start with **Automatic / 自動辨識**. Byte-order marks and strong Unicode byte
patterns take priority. Available choices include UTF-8, UTF-16/32 with byte-order
options, GB18030/GBK, Big5, Windows-1252, ISO-8859-1/15, Shift-JIS/Windows-932,
EUC-JP, ISO-2022-JP, Windows-1251, KOI8-R and ISO-8859-5. HTML can use its declared
charset; an explicit user selection takes precedence. RTF also interprets its own
Unicode and font/code-page controls.

Several legacy encodings can decode the same bytes differently. The application
records that ambiguity and its alternatives rather than presenting language
detection as certain. Inspect the preview, choose the known original encoding
under **Encoding detection and text correction**, and re-extract before confirming
the result. Invalid input is not repaired by silently dropping or replacing bytes.

### Scanned documents and optional components

Choose OCR languages before upload or under the PDF extraction options. The
supported Tesseract language identifiers are `eng`, `chi_sim`, `chi_tra`, `deu`,
`fra`, `jpn`, `rus` and `lat`; a combined selection requires every selected pack.
The default selection uses `chi_sim+chi_tra+eng`. Install Tesseract and the required
language data locally. Missing OCR, language data or PDF rendering produces a
diagnostic; a selectable language is not proof that its pack is installed or that
the document has been recognized accurately.

Run `python critic_runner.py doctor` to see parser and OCR availability and the
declared license boundary for each optional component. The Studio environment
card can repair missing Python adapters in one action (`pypdf` and
`pypdfium2`). These are bundled in the portable release. After a successful repair, an open project that was blocked before
human extraction confirmation is automatically re-ingested; the same retry is
also available as an explicit “重新识别” action. Tesseract is an operating-system program and language-pack choice,
so it is reported with an installation hint rather than silently installed.
The same repair is available from the terminal with
`python critic_runner.py doctor --repair`.
The dependency ranges are in `pyproject.toml`. PDFium renders temporary grayscale
OCR pages without changing original document bytes; page size and pixel limits
are checked before allocating the image. PyMuPDF remains an optional manually
installed compatibility backend and is not included by default.

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
provides copy, previous/next, and selected-profile protocol ZIP controls, and shows every
generated export in an export center. Exported files can be downloaded
individually, and the containing folder can be opened from the local desktop.
As soon as at least one independent AI response has been imported, the browser
can export an **unadjudicated AI review snapshot** without waiting for Finding
decisions. It contains a readable Markdown report, structured JSON, verbatim raw
responses, a manifest, and a ZIP bundle. The import notice and request card also
show the project-relative storage location (`audits/<critic>/`); the export notice
shows the full local output directory. This snapshot is explicitly separated from
the formal post-adjudication export.
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
materialized, the export includes the approved Markdown, Word output, difference
report, unresolved risks and recheck result. Supported original DOCX paragraphs
and simple cells use a source-preserving path. If that path cannot safely apply
the requested changes, the Word file is named **`规范化修改稿.docx`**, or
**`规范化本轮未修改稿.docx`** for a no-change completion. It is never given a
source-preservation claim merely because its extension is DOCX. Consult
`Word导出说明.md` and `track-changes-capability.json` for the actual path and limits.
A human `corrected_action` supersedes the critic's original
instruction, but cannot change text until an exact Hunk is separately approved.
`correct` requires a non-empty bounded corrected action; `accept` alone may
inherit the critic's suggested action.

## Current product limitations

- The supported Word-preservation path keeps untouched OOXML parts and supports ordinary paragraph
  edits, contiguous paragraph replacements and simple single-paragraph cells.
  A separate native Track Changes copy supports paragraph text replacements;
  structural edits, existing revisions, fields, bookmarks, hyperlinks and
  complex cells can prevent this path. The output filename and `Word导出说明.md`
  explicitly report any normalized fallback. Such a fallback can omit original
  images, headers/footers and layout; inspect the original alongside it. Pagination
  and complex documents still need actual Word rendering and visual QA.
- Table containers and page-break anchors are refused. Users can correct Finding
  locations in the UI, which invalidates the old decision, or explicitly select a
  continuous text range. Arbitrary table reshaping and image editing are not supported.
- Each confirmed Action now provides a source/range/decision-bound drafting
  prompt. Import saves the raw response and creates an unapproved Hunk. It never
  supplies an implicit human approval or silently executes a model.
- `studio-manage action PROJECT REQUEST.json` exposes all Studio actions through
  the same validation and mutation services, including external Resolution.
- External Resolution is only complete after the bound response is imported
  and a human confirms every original item. New items are never labelled
  resolved at discovery time: they remain `new-finding-awaiting-next-round`
  until promoted into the next revision round's normal Finding queue.
- If every Hunk is rejected, exports use `本轮未修改稿` and record `completion:
  no-change`, while retaining unresolved risks and all decisions.

## Release evidence and remaining work

The [0.2.3 delivery record](release-engineering-0.2.3.md) covers adversarial deep
review and its verification. The [0.2.2 record](release-engineering-0.2.2.md)
preserves the preceding parser, IR, real Office/OCR and installation evidence.
Those checks do not establish model accuracy, all document layouts or operation
on independent end-user devices. The portable build remains unsigned, and the
independent-author acceptance gate remains open.
