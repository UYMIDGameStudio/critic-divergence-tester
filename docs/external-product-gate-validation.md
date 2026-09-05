# External Product Gate A/B validation

Status: **participant handoff ready; pending external participant evidence**

As of 2026-09-05, the maintainer explicitly deferred human-produced evidence
while code review and engineering improvements proceed. This requirement is
outside the current engineering acceptance scope; no external validation is
claimed and the existing evidence register remains unchanged.

The author-owned Gate A and Gate B decisions prove that their artifact chains and
workflow can be completed; they do not establish independent usability. This
track requires one or two writers or editors who did not author the product or
the manuscript evidence already used for the author gates. Automated fixtures,
the maintainer, and an AI acting as a participant do not qualify.

## Required run

Each participant must use a real manuscript they own or are authorized to edit,
complete either Gate A or Gate B without editing JSON, and make their own
pass/fail/defer decision. Preserve consented evidence under a participant alias;
do not publish manuscript bytes without separate permission.

Record:

- participant alias, role, prior exposure to the product, and consent scope;
- exact Gate type, corpus size, artifact hashes, start/end timestamps, and CLI
  version/commit;
- every request for help, abandoned step, misunderstood label, and repair;
- the participant's assessments and final decision as separate artifacts, never
  copied into or substituted for the author's assessment;
- a side-by-side comparison with the author-owned Gate evidence, including
  disagreements and participant-specific limitations.

## Evidence register

| Participant | Independent writer/editor | Gate | Real corpus | Assessment artifacts | Decision artifact | Compared with author evidence | Status |
|---|---|---|---|---|---|---|---|
| E1 | pending | pending | pending | pending | pending | pending | not run |
| E2 | pending | pending | pending | pending | pending | pending | not run |

The register must remain `not run` until the referenced immutable artifacts can
be verified. A maintainer may not convert an invitation, scheduled session, or
verbal impression into a completed external assessment.

Use `docs/p3-human-handoff.md` as the participant/facilitator boundary. It
specifies qualifying roles, consent scope, allowed help, the no-JSON-edit rule,
and the evidence that must be returned. Preparing or sending that handoff does
not change this register to `run`.

## Completion rule

P3-1 is complete only when at least one qualifying participant has a verified
assessment and decision, recorded separately from the author evidence and made
publicly comparable at the metadata/hash level. Two participants are preferred.
