# Product Gate A protocol

Product Gate A asks whether the Phase 1–3 Workbench is more understandable and controllable than sending a complete manuscript directly to a general chat model, and whether correcting the extracted IR costs less than the value it creates. It is not a benchmark of model truth and never produces a manuscript quality score.

## Required corpus

Use three to five real manuscripts. The author has identified `UYMIDGameStudio/billcharles-blog` as the source repository. The initial author-owned corpus candidates are `content/the-structural-stand-in.md`, `content/the-dynamic-dialectic-of-knowledge-systems-on-change-and-invariance-in-theoretical-identity.md`, and `content/diachronic-continuity-and-argumentative-responsibility-in-knowledge-migration.md`. Their repository presence establishes source ownership, not successful Gate evidence. Do not count the bundled demo fixture as a real manuscript. Keep manuscript workspaces and Gate A evidence outside either source repository.

Candidate discovery was pinned at source commit `0f78443`. Before starting a real run, compare the current source bytes with these discovery hashes; if an article changed, use the new bytes as the manuscript version and let `DocumentVersion` record the new hash rather than silently substituting it.

| Candidate | Discovery SHA-256 |
|---|---|
| `the-structural-stand-in.md` | `e8cec1b2186ed4f61053ed684673adbf28c734553b3cf06b46875f492d706abb` |
| `the-dynamic-dialectic-of-knowledge-systems-on-change-and-invariance-in-theoretical-identity.md` | `92cebd820b2c9487ccd3f250b9bc1c3903c3db276797df7332606ff7e6b53fd1` |
| `diachronic-continuity-and-argumentative-responsibility-in-knowledge-migration.md` | `60dbb82c963b356166a02104a2ab986994f94c04b265ca806de87e8d36b31fb6` |

### Author-run execution status

The private author workspaces and model responses remain outside the repository. This table records only reproducibility metadata and does not count a manuscript as Gate-complete.

| Candidate | Direct baseline | Model | Prompt SHA-256 | Response SHA-256 | Elapsed | Next uncompleted step |
|---|---|---|---|---|---:|---|
| `the-structural-stand-in.md` | controlled `DB1` | OpenAI `gpt-5.6-sol`, high reasoning | `61d8b971ae0d6bd9a6b67ad98d89a00b44bcfc8206543f791efcb374aa0459cd` | `891b438bde3246e0f91dbadb77c77a64eda120ae5da8830c0d8d523f2a335a90` | 293,097 ms | collect Raw IR |
| `the-dynamic-dialectic-of-knowledge-systems-on-change-and-invariance-in-theoretical-identity.md` | controlled `DB1` | OpenAI `gpt-5.6-sol`, high reasoning | `8aab782a912a27b8259afda484865461a90e7b90144684cafa44bd47761466bc` | `53e6e7479ac3aa496b6fe7aa89be27e90212b5177dac2619badb5de7f5380e43` | 247,238 ms | collect Raw IR |
| `diachronic-continuity-and-argumentative-responsibility-in-knowledge-migration.md` | controlled `DB1` | OpenAI `gpt-5.6-sol`, high reasoning | `59b244e8cfcb191fd97aeec221a213d99aa1a89f406219218d5aebe4ed183745` | `41fdd7adb68488bf423e3e4bef58b24766c63cacfe04ad0ece450795e3deb05a` | 259,309 ms | collect Raw IR |

All three responses are immutable model-derived comparison artifacts, not accepted reviews and not evidence that their citations or factual assertions are correct. No Workbench IR or Finding for any manuscript influenced its fresh-session response.

Every manuscript must complete the same Phase 1–3 path:

1. in a fresh conversation with no prior context, preserve one direct full-text chat prompt, raw response, provider/model ID, full-manuscript delivery declaration, and actual elapsed time before Workbench Findings influence the evaluator;
2. import the exact source and collect a Raw IR model response;
3. start an `ir-inspection` work session, inspect and correct the IR without editing JSON, then finish that exact session so correction burden has system-timed evidence;
4. prepare a scoped v3 Rule Review and collect at least one current result with relation-bound PASS support paths;
5. acknowledge or reject every non-evaluated execution/routing status through the separate human triage queue;
6. adjudicate every evaluated FAIL/substantive UNCERTAIN Finding as accept, reject, or defer;
7. create the revision plan and verify the whole Workbench project.

Generate the same versioned direct-review protocol for every project with `ir gate-a prepare-baseline PROJECT PROMPT.md`. It embeds the bound source bytes verbatim and refuses to overwrite an existing prompt. Run that exact prompt in a fresh model conversation, then collect it with `ir gate-a baseline PROJECT --prompt-file ... --response-file ... --model-label ... --model-provider ... --model-id ... --interaction-mode fresh-session --prior-context none --manuscript-delivery inline --full-manuscript-confirmed --started-at ... --completed-at ...`. Inline collection and later verification reject a prompt that does not actually contain the exact manuscript bytes. Then run `ir gate-a readiness PROJECT...`. Readiness reports model Findings, Finding adjudications, execution-status triage, baseline control failures, and IR-inspection timing separately without creating an assessment or Gate decision. `ir gate-a init` refuses missing or uncontrolled baselines, duplicate sources, invalid workspaces, missing revision plans, open Findings, open triage items, open work sessions, or a missing pre-review IR-inspection session. The resulting v5 corpus is immutable and binds the exact baseline, triage indexes, and eligible work-session records.

Wrap human activities with `ir gate-a session start PROJECT --activity ir-inspection` and `ir gate-a session finish PROJECT GS1`; use `session list` to inspect open and completed intervals. The session artifacts use actual system timestamps and enter project verification. Finish IR inspection before collecting the first Rule Review result. Gate v5 binds those exact completed records, derives elapsed milliseconds, and rejects self-reported correction minutes. Historical v1–v4 artifacts remain verifiable without retroactive rewriting.

## Engineering regression corpus

The five peer-reviewed papers below are an engineering regression corpus, not a completed Product Gate. They exercise extraction, routing, research-design diversity, and task volume. Because the evaluator is not their author and will not produce authentic V2 revisions, they cannot establish that authors prefer the Workbench to direct chat.

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
- exact system-timed IR-inspection milliseconds (the report also renders minutes for readability), derived from the bound pre-review work-session records rather than entered by the evaluator;
- missed Claims, wrong Claim types, wrong relations, rhetoric extracted as Claims, and reversed attribution counts;
- at least one known regression anchor, such as an important Claim, extraction trap, expected Finding, actual adjudication, or framework reversal;
- what the author actually changed, if the observation extended through revision;
- contextual notes explaining the counts.

Counts are human observations, not automatic truth labels. Known anchors are not a complete gold standard. They exist to ensure important structures do not disappear unnoticed after later changes.

## Decision

The deterministic report may establish that evidence is complete, but it cannot decide whether the evidence is good enough. A human evaluator chooses `pass`, `fail`, or `defer` and supplies a reason. `pass` is structurally blocked until all workspaces and assessments are complete, but structural readiness does not imply that passing is substantively warranted.

If correction burden is high, important Claims are routinely missed, attribution reversals remain common, or the workflow is not clearer than direct chat review, choose `fail` or `defer`. Improve extraction/correction UX and repeat Gate A. Do not enter Phase 4 merely because the commands and tests work.

## Current limitation

The private source corpus and five V1 Workbench projects have been initialized and hash-verified. A model-derived Raw IR attempt was collected for every paper, and each deterministic Reviewed IR cache and argument map rebuilt byte-for-byte without any human correction events. The initial Reviewed IR contained 66 Claims in total, but classified many Claims under two types and two methods. A second immutable model-derived attempt retained the Claim text and relations while proposing one primary type and method per Claim. It did not create or impersonate a human correction.

| Alias | Claims | Initial Reviewed IR SHA-256 | Current model-refined Reviewed IR SHA-256 |
|---|---:|---|---|
| P01 | 15 | `0867aecfd03fbc77a1d6ab9697f562cb62a11b36dae5d654579166f2b83d7b6f` | `66edb519f590bc873aa8aff198817dbaa6f46c33416eac712a8d670198d64871` |
| P02 | 13 | `c2f5567258321c3dd743d5a5741255b59f975d5a66d189a5e55b80293c97676d` | `7e7ec8465c49cf20968854fdf6aa9945f3a5056ea5564f186165bd703d95a16c` |
| P03 | 14 | `3278c5d2cc398e0a13090ce327a6317090817e5ab9e5525fb79135239d3e5ea4` | `9cad7859e5156f6fbfa9c95fb519ed5c4118a619e72492508f79b67d213ebb1c` |
| P04 | 13 | `d4a139fc9b5357e00e5b327dd7d3ce9323bcbec514b10b349fcb87a6a2b8846e` | `2da1ea3f8e7d3df7143fb65fcf5b34b7d16cb2145092960d7a528a50da15c1e5` |
| P05 | 11 | `4e3f994aa481b67494f7faf16925c6f307b5f5d8e5e993d8278d18d5cac7201f` | `9e39775c7551c198608c5bda8a275aa13b3b2257af846f73d371ab1c8ac70a62` |

The first full Rule Review plan produced 704 tasks; the first core plan still produced 582. That real-corpus result exposed an applicability defect: empirical temporal-order, confounding, reverse-causality, and selection-bias checks were also being assigned to conceptual and formal-model causal Claims. Restricting those four checks to observational and experimental methods reduced the immutable core plans to 478 tasks while leaving the randomized field experiment P05 unchanged. Re-running against the model-refined classifications reduced the current core plans to 316 tasks. Every earlier Raw attempt and Rule Review snapshot remains present and hash-bound.

| Alias | Current core tasks | PASS | FAIL | UNCERTAIN | Open Findings |
|---|---:|---:|---:|---:|---:|
| P01 | 81 | 50 | 6 | 25 | 31 |
| P02 | 54 | 36 | 6 | 12 | 18 |
| P03 | 59 | 30 | 7 | 22 | 29 |
| P04 | 75 | 49 | 9 | 17 | 26 |
| P05 | 47 | 35 | 4 | 8 | 12 |
| **Total** | **316** | **200** | **32** | **84** | **116** |

These legacy v1 verdicts are model-derived proposals, not accepted criticism. Their 200 PASS outcomes predate `basis_refs`/`support_refs` and the explicit evidence policies, so they remain reproducible historical artifacts but cannot establish strong PASS evidence. No IR correction, Finding adjudication, revision action, direct baseline, usability assessment, or Gate decision has been attributed to a human. Product Gate A remains incomplete, and Phase 4 remains blocked.

The convergence iterations address the observed cost and audit gaps directly: daily review defaults to the thesis support chain rather than all Claims; v3 PASS evidence binds an allowed directed relation path; execution/routing states enter a separate append-only human triage queue; and Gate comparisons bind an immutable, controlled direct-chat baseline plus the resulting triage indexes. The five engineering projects should be rerun with v3 only as a regression smoke test. The three author-owned candidates above must still receive fresh baseline runs, real corrections, triage decisions, adjudications, RevisionActions, and later revisions before they count as a Product Gate corpus.

A read-only v2 plan dry-run on the existing model-refined IRs selected 61 of 66 Claims and 294 core tasks, versus 316 under `scope=all` (P01 75/81, P02 54/54, P03 59/59, P04 75/75, P05 31/47). This is only a modest reduction because the extractor labeled 37 of 66 Claims as `intermediate`, and most premises are connected to a conclusion chain. Scope is now controllable and P05 benefits materially, but the dry-run does not show that the default queue is yet affordable. Human role correction and explicit `claim/claims` scoping must be measured in the author-owned Gate corpus rather than hidden by another automatic heuristic.
