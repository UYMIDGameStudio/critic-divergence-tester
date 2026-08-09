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

## Selected real-paper corpus

The first private Gate A run uses five peer-reviewed, openly accessible papers with complementary argument forms. Full text and Workbench directories remain outside the repository. The public record contains only bibliographic metadata, licenses, and SHA-256 bindings for the deterministically derived UTF-8 manuscript inputs.

| Alias | Paper and argument form | License | Manuscript SHA-256 |
|---|---|---|---|
| P01 | [Ioannidis (2005), *Why Most Published Research Findings Are False*](https://doi.org/10.1371/journal.pmed.0020124) — conceptual/statistical essay | CC BY 4.0 | `89bb35f3f047fa1145f20b48f29967be56dd82dc12d5c953f668183d25bf9f0a` |
| P02 | [Smaldino & McElreath (2016), *The natural selection of bad science*](https://doi.org/10.1098/rsos.160384) — empirical review plus formal evolutionary model | CC BY 4.0 | `5089e97af461ab827e95dba0ecc49f3439efe60569aded8c0127f920784eb7fe` |
| P03 | [Munafò et al. (2017), *A manifesto for reproducible science*](https://doi.org/10.1038/s41562-016-0021) — methodological synthesis and policy argument | CC BY 4.0 | `ebcc41a6fdd66dea5c004aab054498be2c9e15b16b7b6f25092d2763ec9b2a43` |
| P04 | [Szucs & Ioannidis (2017), *Empirical assessment of published effect sizes and power in the recent cognitive neuroscience and psychology literature*](https://doi.org/10.1371/journal.pbio.2000797) — large-scale empirical meta-research | CC BY 4.0 | `229cfdff95e35aa487bc79f5f1f7126aa2f065b567e7a6843fe00726b7d08f2b` |
| P05 | [Bail et al. (2018), *Exposure to opposing views on social media can increase political polarization*](https://doi.org/10.1073/pnas.1804840115) — randomized social-media field experiment | CC BY-NC-ND 4.0 | `039f15a15cce0a4651e502905adbe354890cfaac6ac9caeea06c863163192a8c` |

This selection deliberately exercises nested formal assumptions, simulation-to-world inference, parallel recommendations, quantitative denominator and scope questions, null/asymmetric results, causal alternatives, and external-validity qualifications. These papers are evaluation inputs, not presumed gold truth. Their Argument IR, Findings, adjudications, and usability assessments must still be inspected by a human.

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

The private source corpus and five V1 Workbench projects have been initialized and hash-verified. A model-derived Raw IR attempt has been collected for every paper, and each deterministic Reviewed IR cache and argument map rebuilds byte-for-byte without any human correction events. The current Reviewed IR contains 66 Claims in total:

| Alias | Claims | Reviewed IR SHA-256 |
|---|---:|---|
| P01 | 15 | `0867aecfd03fbc77a1d6ab9697f562cb62a11b36dae5d654579166f2b83d7b6f` |
| P02 | 13 | `c2f5567258321c3dd743d5a5741255b59f975d5a66d189a5e55b80293c97676d` |
| P03 | 14 | `3278c5d2cc398e0a13090ce327a6317090817e5ab9e5525fb79135239d3e5ea4` |
| P04 | 13 | `d4a139fc9b5357e00e5b327dd7d3ce9323bcbec514b10b349fcb87a6a2b8846e` |
| P05 | 11 | `4e3f994aa481b67494f7faf16925c6f307b5f5d8e5e993d8278d18d5cac7201f` |

The first full Rule Review plan produced 704 tasks; the first core plan still produced 582. That real-corpus result exposed an applicability defect: empirical temporal-order, confounding, reverse-causality, and selection-bias checks were also being assigned to conceptual and formal-model causal Claims. Restricting those four checks to observational and experimental methods reduced the current immutable core plans to 478 tasks while leaving the randomized field experiment P05 unchanged.

| Alias | Current core tasks | PASS | FAIL | UNCERTAIN | Open Findings |
|---|---:|---:|---:|---:|---:|
| P01 | 112 | 67 | 6 | 39 | 45 |
| P02 | 82 | 45 | 8 | 29 | 37 |
| P03 | 87 | 40 | 7 | 40 | 47 |
| P04 | 99 | 63 | 9 | 27 | 36 |
| P05 | 98 | 53 | 6 | 39 | 45 |
| **Total** | **478** | **268** | **36** | **174** | **210** |

These verdicts are model-derived proposals, not accepted criticism. No IR correction, Finding adjudication, revision action, usability assessment, or Gate decision has been attributed to a human. The 210 open Findings are a preliminary human-cost warning and may justify another extraction/review UX iteration; they are not permission to auto-reject uncertainty or auto-pass Gate A. Product Gate A remains incomplete, and Phase 4 remains blocked until a human completes the required correction, adjudication, and assessment work.
