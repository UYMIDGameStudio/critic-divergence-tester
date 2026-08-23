# Argument Workbench productization baseline

Baseline date: 2026-08-23 (Asia/Shanghai)  
Audited commit: `34cf448` (`origin/main`)  
Platform: Windows, Python 3.14.7

## Verification result

`python -m unittest discover -s test -v` completed 213 tests in 46.968 seconds: all passed, with three environment-specific skips. The local TCP UI test was skipped because the execution sandbox blocks local TCP; two platform/attack tests were skipped by their own guards.

The historical `34cf448` reference in the product brief is also the current remote `main` at audit time. The local feature branch was fast-forwarded to that exact commit before product work began.

## Actual current flow

The supported Workbench path is manuscript import → model-assisted Argument IR extraction → human IR correction → rule or Perspective Lens review → Finding adjudication → RevisionAction plan → externally authored V2 import → structural diff and semantic lineage → original-Lens Finding Resolution. A token-protected loopback UI exists after Reviewed IR has been created, but it is a document/review viewer and adjudicator, not yet a complete revision workbench.

The ordinary-author gaps at baseline are:

- no top-level `app` entry or create/resume application shell;
- no direct “manuscript + existing free-form review report” path;
- no constrained revision-generation run, raw result attempt, validated patch proposal, per-hunk decision, or deterministic application record;
- no UI path from selected Findings through diff approval to immutable V2;
- README still recommends a multi-command IR workflow;
- no `LICENSE` file;
- Gate A/B measure earlier research questions, not the requested end-to-end Gate C usability comparison.

## Reuse map

- immutable V1/V2 storage and parent/hash bindings: `argument_workbench.py`;
- strict JSON parsing, atomic writes, provenance helpers, and artifact validation conventions: `argument_workbench.py` and `argument_contracts.py`;
- Claim-centered rule/Perspective findings and append-only human adjudication: `argument_review.py`, `argument_perspective.py`, and `argument_adjudication.py`;
- structural V1/V2 diff and semantic Claim lineage: `argument_versioning.py` and `argument_lineage.py`;
- original-Lens retest and human-confirmed resolution: `argument_resolution.py`;
- loopback-only, token-protected HTTP foundation: `argument_ui.py`.

## Compatibility decision

The existing v1-v3 IR, review, lineage, citation, and resolution contracts remain unchanged. Productization adds a separate revision workflow module whose artifacts bind existing DocumentVersion bytes and accepted Finding/RevisionAction identifiers. The UI delegates mutation to this domain module; it does not implement looser validation in HTTP or JavaScript.

No new critic, Gate, score, or Lens is introduced. Existing projects remain readable. Product-only artifacts are additive, so older projects require no speculative migration.
