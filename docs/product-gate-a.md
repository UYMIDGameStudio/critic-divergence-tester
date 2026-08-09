# Product Gate A protocol

Product Gate A asks whether the Phase 1–3 Workbench is more understandable and controllable than sending a complete manuscript directly to a general chat model, and whether correcting the extracted IR costs less than the value it creates. It is not a benchmark of model truth and never produces a manuscript quality score.

## Required corpus

Use three to five real manuscripts. Include writing that has already received close human analysis when possible; the planned corpus should include 《结构的替身》 if its author can supply it. Do not count the bundled demo fixture as a real manuscript. Keep unpublished manuscript workspaces and Gate A evidence outside the repository.

Every manuscript must complete the same Phase 1–3 path:

1. import the exact source and collect a Raw IR model response;
2. inspect and correct the IR without editing JSON;
3. prepare and collect at least one current Rule Review;
4. adjudicate every FAIL/UNCERTAIN Finding as accept, reject, or defer;
5. create the revision plan and verify the whole Workbench project.

`ir gate-a init` refuses fewer than three or more than five projects, duplicate source bytes, invalid workspaces, missing revision plans, and open Findings. The resulting corpus is immutable. If a bound workspace changes, create a new corpus rather than silently updating the evidence.

## Human assessment

For each corpus alias, record:

- whether the workflow was clearer/more controllable than direct full-text chat review;
- whether IR correction burden was acceptable, high, or uncertain;
- correction time in minutes;
- missed Claims, wrong Claim types, wrong relations, rhetoric extracted as Claims, and reversed attribution counts;
- at least one known regression anchor, such as an important Claim, extraction trap, expected Finding, actual adjudication, or framework reversal;
- what the author actually changed, if the observation extended through revision;
- contextual notes explaining the counts.

Counts are human observations, not automatic truth labels. Known anchors are not a complete gold standard. They exist to ensure important structures do not disappear unnoticed after later changes.

## Decision

The deterministic report may establish that evidence is complete, but it cannot decide whether the evidence is good enough. A human evaluator chooses `pass`, `fail`, or `defer` and supplies a reason. `pass` is structurally blocked until all workspaces and assessments are complete, but structural readiness does not imply that passing is substantively warranted.

If correction burden is high, important Claims are routinely missed, attribution reversals remain common, or the workflow is not clearer than direct chat review, choose `fail` or `defer`. Improve extraction/correction UX and repeat Gate A. Do not enter Phase 4 merely because the commands and tests work.

## Current limitation

The repository contains tooling and synthetic tests for this protocol, but no private real-manuscript corpus. Therefore the existence of this module is not evidence that Product Gate A has passed.
