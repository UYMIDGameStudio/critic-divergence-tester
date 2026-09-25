# 0.2.3 — adversarial deep review

Status: **experimental preview**, unsigned. This release adds a usable, traceable
adversarial workflow; it does not establish improved model accuracy or complete
the independent-author commercial acceptance gate.

## User-visible behavior

- Academic and document findings can receive a separately produced defense and
  evidence assessment. Each stage reads the same complete structured manuscript
  and the original critic's preserved standard.
- Assessments recommend retain, narrow, withdraw or insufficient. Human finding
  decisions, approved edits and resolution decisions remain separate.
- Traditional Chinese and English interfaces provide stage prompts, exact downloads,
  response import, source anchors, error recovery and an explicit restart retaining
  prior records. Text from manuscripts and models is displayed as untrusted text.
- Current external findings inherited from a revision are eligible too. Local
  deterministic prechecks are not mislabeled as independent model challenges.
- Protocol ZIPs, AI reports, formal audit reports and project backups carry the
  relevant records. Follow-up AI results can be exported before new adjudication.

The [workflow guide](adversarial-review.md) explains the stages and their limits.

## Contracts and integrity

Protocol/schema validation is in `document_review_adversarial.py`; persistence and
stage transitions are in `document_review_stores/adversarial.py`; the browser flow
is in `studio_web/adversarial.js`. These reuse the existing project transaction,
integrity register, source model and browser request receipts.

Sessions bind the actual finding contents and source artifact, source bytes,
structured manuscript, confirmed context, review round and original critic
protocol. Separate stage requests bind the prior session/result. Response contracts
are versioned; exact quotations and referenced defense evidence IDs are checked.
Provider/model values are declared metadata, not authenticated execution receipts.

Changed findings, corrected locations, superseding sessions and later review rounds
invalidate pending old responses. Accept/reject/defer alone does not change the
question being examined. Successful retries are idempotent; rejected responses are
archived and deduplicated without advancing the stage. Invalid envelopes, invented
quotes, unknown fields and attempts to write human decisions are rejected.

Damaged exchange artifacts cause read-only protection while keeping the diagnostic
and recovery interface accessible. They are not projected as trusted review results.

Release checks also found that reusing a populated local build directory could
include old `__pycache__` files in a wheel. The wheel builder now uses fresh staging
directories and validates packaged content before publishing. Portable web assets
are selected explicitly instead of recursively copying their cache directories.

## Compatibility and delivery

Project schema remains 1. Existing projects without adversarial records do not need
conversion, and earlier critic prompts are not rewritten. Use 0.2.3 or later for new
deep-review sessions; restore a pre-upgrade backup into a separate library when
rolling back the application.

The portable runtime self-test now creates a synthetic external challenge, imports
both stages and verifies that human decision authority is unchanged. This checks
the installed product and packaging, not the validity of a real review.

## Verification boundary

Local source acceptance on 2026-09-25: 682 regression tests completed, 679 passed
and 3 skipped (two platform-specific checks and the separately invoked real-bundle
installation test). The new workflow contributes 21 backend and 11 delivery tests,
including transaction rollback and unchanged-source/new-round rejection.

Seven browser groups passed in Chrome 153.0.8010.53. The deep-review group covers
13 cases, including both interface languages, malformed response recovery,
interrupted-request replay, retained drafts, inert model markup and old-finding
history. This verifies browser behavior, not model accuracy.

The clean wheel contains 121 members, with 115 source/data files checked against
the checkout. It passed isolated installation and the installed-runtime self-test
in a fresh environment without optional PDF/OCR dependencies. The portable builder
also requires its embedded runtime self-test before publishing; actual bundle
installation is tested separately after the build.

Release outputs and their final hashes are in `dist/delivery-0.2.3/`, with the
machine-readable acceptance record `FINAL-VERIFICATION.json` alongside the wheel
and Windows portable bundle. Those generated files stay outside Git.

No external model was invoked by the automated fixtures, and no participant or
independent-author outcome is inferred from their success.

Still pending: independent real-manuscript comparisons, 3–5 uninvolved authors'
acceptance sessions, signing, and validation on independent end-user devices.
The user currently cannot arrange those participants; the product does not replace
that missing evidence with synthetic test results.
