# Document Review Studio

> Status: **experimental preview**. Do not present this branch as a formal V1
> until the constrained Finding → Action → Hunk → Resolution loop produces and
> verifies a genuinely revised document.

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

The UI flow is intentionally gated:

1. Upload `.md`, `.txt`, `.docx`, or `.pdf`.
2. Inspect extraction quality and warnings.
3. Confirm, correct, continue with warning, or replace the upload.
4. Confirm document type, jurisdiction, effective date, publisher, audience,
   publication status, and risk-domain context.
5. Run the clearly labelled deterministic local precheck, or export five
   critic-specific AI protocols and import each raw JSON response.
6. Decide each Finding manually.
7. Prepare the bridge into the existing constrained revision workflow.
8. Export the audit package and editable draft.

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
run/Finding, sequenced human decision, revision bridge, and export. Missing
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
declared license boundary for each optional component. The dependency ranges
are in `pyproject.toml`; PyMuPDF's AGPL/commercial choice must be reviewed for
the deployment context.

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
```

The exported prompt includes a required response envelope containing the exact
`request_id`, `prompt_sha256`, provider, and model. Imports reject a response
unless all four values echo the selected request. `prompt_sha256` identifies
the protocol payload; `prompt_file_sha256` separately binds the rendered prompt
file. The current browser/CLI flow is a manual import, so parsed runs store
`declared_model_metadata` rather than claiming a direct `model_invocation`.
Imports preserve the declared metadata, raw response hash/content, and parsed
AuditRun/Finding separately. `collect_model_audit` validates the request
envelope, source hash, critic identity, Finding contract, and block locations.

For DOCX inputs, preview exports are named
`normalized-editable-copy.docx`. They may lose source layout and are not a
revision. `revised.docx` is deliberately unavailable until the constrained
revision chain is complete and verified. A human `corrected_action` is carried
into the revision bridge and supersedes the critic's original suggestion.
