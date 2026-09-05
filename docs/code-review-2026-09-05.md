# Engineering review — 2026-09-05

Scope: review the current worktree, repair concrete defects, and improve the
implementation following the earlier module split. At the maintainer's request,
human-produced P3 evidence is deferred. No human Gate or blind-pairing result is
inferred from automated checks.

## Findings and fixes

| Priority | Finding and trigger | Resulting behavior | Regression evidence |
|---|---|---|---|
| P1 | The packaging configuration listed only the old flat modules; a wheel omitted `cli`, `contracts`, the Studio stores, and new shared modules. Source-tree tests concealed the missing imports. | Explicitly package the three new families and shared modules. CI installs the package and runs with `python -I` to exclude checkout imports. | `scripts/verify_installed_package.py`: imports, installed protocol and rule data, CLI, and a complete local Studio precheck workflow. |
| P1 | Gate initialization removed its recovery marker before rename, and generated reports after publication. A kill or report exception could leave unidentifiable staging or a published incomplete Gate. | Reserve a deterministic sibling staging directory, recheck the destination under the parent lock, and build reports before atomic publication. Legacy cleanup matches literal Gate names and the exact old marker. | `test/test_argument_gate_common.py`: real process exits after directory creation, during work, and just before publication; retry, destination race, and glob isolation. `test/test_argument_gate_b.py`: report generation failure leaves no published Gate and retry verifies. |
| P1 | Windows case aliases did not share the same reentrancy key; initial lock-file writes preceded acquisition. Symbolic/hardlinked lock carriers could target a different file. | Normalize Windows path keys, acquire before initializing the lock byte, and reject linked/nonregular carriers. Propagate unrelated OS failures with their actual cause. | `test/test_project_lock.py`: case-alias nesting, cross-process contention, hardlink target preservation, OS-error identity, and reentrancy. |
| P1 | Each campaign repetition reopened the manuscript and protocol. Editing or removing them mid-campaign could mix inputs or leave the campaign unfinished. | Freeze source and protocol bytes at campaign start and reuse those snapshots for all repetitions. Report start/end progress on stderr, retaining the stdout path contract. | `test/test_campaign_snapshot.py`: the executor removes the original source during the first run; all four runs retain the same source digest and the intended protocol snapshot. Existing campaign tests cover archive verification and scoring contracts. |
| P1 | DOCX declaration checks scanned ASCII bytes only; UTF-16 DTD declarations bypassed that scan. Corrupt DEFLATE could escape as a raw decompressor error. | Reject DTDs at the XML parser target, regardless of declaration encoding; retain valid UTF-16 support. Normalize corrupt streams to ingestion errors and enforce expansion limits during reading. | `test/test_document_review_ingest.py`: UTF-16 entity declaration rejection, valid UTF-16 parsing, corrupt-stream handling, byte-count limits, and archive closure on early rejection. |
| P2 | Studio copied its entire globals dictionary into five modules for every delegation, overwriting their `__name__`, `__spec__`, and `__file__`. The thread-lock registry also retained every visited project indefinitely. | Refresh only referenced dependencies for the current component and shared decorator module; retain facade patch compatibility and original module metadata. Release unused thread-lock registry entries through weak references. | `test/test_document_review_components.py`: metadata and patch/restore behavior. `test/test_project_lock.py`: registry lifecycle. |

## Verification

- Baseline before this review: 308 tests, no failures, four conditional skips.
- Full regression after the fixes: 322 tests, no failures, four conditional skips
  (113.287 seconds on local Windows / Python 3.14).
- After narrowing Studio dependency refresh to the active component, the
  complete Studio tests plus the new component tests passed: 61 tests, no
  failures, two conditional skips (59.375 seconds).
- Built and installed the actual wheel in a separate virtual environment;
  `python -I scripts/verify_installed_package.py` passed outside the source tree,
  including a rebuild and reinstall after the final component optimization.
- Both previously completed model campaign archives still return `verified`
  against their source manuscripts. Their human-pairing state remains pending.
- `git diff --check` passes. CI now exercises installed-package behavior on both
  Windows and Ubuntu, alongside the existing Python 3.10/3.14 matrix. Remote CI
  and native POSIX lock execution were not run locally in this review.

A small local timing check of 10,000 binding refreshes measured approximately
0.035 seconds for copying all namespaces and 0.009 seconds for the targeted
ingestion-component refresh. This measures that helper only, not end-to-end
page latency or a guaranteed speedup on other machines.

The work remains in the local worktree for review; this pass does not publish
the repository or create external messages. Remaining product limitations,
including human validation and native DOCX revision fidelity, remain documented
in the product documentation.
