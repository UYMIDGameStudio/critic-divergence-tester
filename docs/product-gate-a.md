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
| `the-structural-stand-in.md` | controlled `DB1` | OpenAI `gpt-5.6-sol`, high reasoning | `61d8b971ae0d6bd9a6b67ad98d89a00b44bcfc8206543f791efcb374aa0459cd` | `891b438bde3246e0f91dbadb77c77a64eda120ae5da8830c0d8d523f2a335a90` | 293,097 ms | human triage and Finding adjudication |
| `the-dynamic-dialectic-of-knowledge-systems-on-change-and-invariance-in-theoretical-identity.md` | controlled `DB1` | OpenAI `gpt-5.6-sol`, high reasoning | `8aab782a912a27b8259afda484865461a90e7b90144684cafa44bd47761466bc` | `53e6e7479ac3aa496b6fe7aa89be27e90212b5177dac2619badb5de7f5380e43` | 247,238 ms | human triage and Finding adjudication |
| `diachronic-continuity-and-argumentative-responsibility-in-knowledge-migration.md` | controlled `DB1` | OpenAI `gpt-5.6-sol`, high reasoning | `59b244e8cfcb191fd97aeec221a213d99aa1a89f406219218d5aebe4ed183745` | `41fdd7adb68488bf423e3e4bef58b24766c63cacfe04ad0ece450795e3deb05a` | 259,309 ms | human triage and Finding adjudication |

All three responses are immutable model-derived comparison artifacts, not accepted reviews and not evidence that their citations or factual assertions are correct. No Workbench IR or Finding for any manuscript influenced its fresh-session response.

### Raw IR extraction status

Each candidate now has one immutable, source-bound `attempt-0001`. The Reviewed IR hashes below are the deterministic zero-correction replay of those attempts; they are not yet human-confirmed. Raw responses and private workspaces remain outside the repository.

| Candidate | Raw response SHA-256 | Raw record SHA-256 | Initial Reviewed IR SHA-256 | C / E / A / Z / R | Next uncompleted step |
|---|---|---|---|---:|---|
| `the-structural-stand-in.md` | `7583e1a72f4b83e331026adbaab88909a3c1e6d2e5bb8187f4d07da083b8a1cd` | `5b835416de7337da5fa803cd1906ca6da32395bf915a7fe7497bae981d76569d` | `003f4e0367e39a8748c0be5fc69a9a80307a1fdcd7418b5c7bb73ec88679830a` | 13 / 7 / 2 / 4 / 25 | human triage and Finding adjudication |
| `the-dynamic-dialectic-of-knowledge-systems-on-change-and-invariance-in-theoretical-identity.md` | `dacd887daaef667e1f3dd92afab5dc74c5279e0f49a045ff3a3fbba74a8e7557` | `312c749b5fba9aee5fea9832e49979aad5c07fd7150d8a80ad6e05a2085adbad` | `3e818b5f3fcf09ee6b49ff2ad9a507a4c1408b208d54f6cc84d8e1379f00d707` | 12 / 5 / 2 / 7 / 27 | human triage and Finding adjudication |
| `diachronic-continuity-and-argumentative-responsibility-in-knowledge-migration.md` | `9a00b13c3db957e2152196e4d2a6afc19888175fc9c885ab45df969c222994b8` | `cb519288d62b1a96081d4c80de4b8219bf5d6b94cc9c0ff59ba562d2465818d2` | `212024271d1fab7d8b04a5331be76e9581b2b8394e4a030d7fb33723ba5aabd0` | 13 / 6 / 2 / 6 / 27 | human triage and Finding adjudication |

The extraction producer label records OpenAI `gpt-5.6-sol` with high reasoning. It identifies model-derived output only; it does not human-confirm any node, relation, citation, or uncertainty entry.

### Human IR inspection status

The first author inspection was completed through the line-oriented Inspector without editing JSON. The measured duration includes initial UI confusion, the clarification of what IR deletion means, and the guided node-by-node review; it must not be interpreted as pure editing time.

| Candidate | Session | Exact elapsed | Correction events | Net result | Reviewed IR SHA-256 | Reviewed record SHA-256 | Next uncompleted step |
|---|---|---:|---:|---|---|---|---|
| `the-structural-stand-in.md` | `GS1` (`bc75d31b714c6708f944aaf259eb949707294f79f7eb4a3135993202a694a12c`) | 5,899,678 ms | 5 | 13 → 10 Claims; three deletions, one deletion later reverted; all other nodes and 18 surviving relations retained; E2/E3/Z2 human-confirmed as intentionally unbound | `8d01bea49786f872216fdbcc215143c8aabad0b418c6c9a79b2ea69d26459ebb` | `a4d517a6dcecbc7b17f72113214e9c01dadfcf434a300c880a84e938cff7dafa` | human triage and Finding adjudication |
| `the-dynamic-dialectic-of-knowledge-systems-on-change-and-invariance-in-theoretical-identity.md` | `GS2`–`GS6` (five records) | 129,752 ms | 1 | all 12 Claims and all Evidence/Assumptions/Citations retained; model-proposed `C1 → C2` removed, leaving 26 relations | `50f943938e52a83f8a4d44f9133ad22c851e020570e6214a8789a01ec0eb820e` | `48e9be5467b4a607958dbbb7962b0037d448f9390f07c01f109f019f0ee4121e` | human triage and Finding adjudication |
| `diachronic-continuity-and-argumentative-responsibility-in-knowledge-migration.md` | `GS1`–`GS4` (four records) | 15,044 ms | 0 | all 13 Claims, all other nodes, and all 27 relations retained | `212024271d1fab7d8b04a5331be76e9581b2b8394e4a030d7fb33723ba5aabd0` | `6a345b15288fa22864eaad5e2cd7068cfb9357ef81c0f70a4162db6b1c7116d7` | human triage and Finding adjudication |

The P1 correction chain consists of immutable `IC0001`–`IC0005`; `IC0005` reverts `IC0004` rather than erasing it. The final Reviewed record binds the ordered correction hashes. The session and final Reviewed artifacts both pass `ir verify-project`.

The P2 inspection was deliberately split into five short completed sessions so conversational waiting was not charged as correction burden. Their exact record hashes are `8a0c9bb176ad89032d604e9f41283d7ce0d296df44756f66d689792223bfb322`, `9010d6c8ae1f648e80f589ff5daa3c528d0824f8060cb35d1226aac3090aa35d`, `ff69fea0c5525e44c00110692b9c5b1940189be75924b1d70dc2db3330745925`, `065879a0ddfbf5e952eb5e9cf199f2dbf005a3fb43518bf379751466a6ae9fb2`, and `ee1c0d443b86446031a907899b055fce8ad7abb64b6bbe524eba3f1feb4a50ab`. An earlier 307,583 ms interval is preserved as `gate-a-session-abandonment` (`df1bc5dce50530bf91dfdc3d9a1b036714a556b358fd140fab279964b69a75df`) and excluded from the total because no author judgment occurred during it. The sole correction, `IC0001` (`7bc2c2696141533cd00c9721d2789ad79a62dfa368f8052edcfa5e18c7df033c`), removes `R1`; it does not relabel unchanged model-derived fields as deterministic or human-origin facts. The project verifies successfully.

The P3 inspection is bound by completed sessions `GS1`–`GS4` with record hashes `4b23230e8cedbd9a915983f6c8969cc872c0d50751c12574bac46fcdf9feb7cb`, `f3982c5091893580580af17127a0ed5bcd0608f74421b0033623169e4b8d8b51`, `27a1b93b30a6c769b25bf58e9b4b927c0a70f4e020f5e6bca86ccc3d162ad8bb`, and `88e3eb84fc434fd83d407922484d2e83ed3cef9ac49ca9737f6e6943a70ccebe`. The author retained the complete extraction, so Raw and Reviewed payload bytes are identical; their semantics remain visibly model-derived because inspection without correction does not rewrite field provenance as human origin. The project verifies successfully.

### Author Rule Review status

All three author manuscripts now have current `core` / `thesis-chain` Rule Reviews against their human-reviewed IRs. P1's plan contains 44 tasks from 16 checks; P2's contains 54 tasks and excludes isolated C1 after the author removed its only support relation; P3's contains 61 tasks across all 13 Claims. The model results are immutable proposals produced by OpenAI `gpt-5.6-sol` with high reasoning; no verdict, Finding, or routing status in these runs is human-confirmed.

| Candidate / review | Plan SHA-256 | Run SHA-256 | Current response SHA-256 | Current attempt SHA-256 | Derived index SHA-256 |
|---|---|---|---|---|---|
| P1 / `RV1` | `a45b3fc639e1800e3f63785af97d6ea19107c7df3bda46c8a52806ec54d54b87` | `98ed031645dbd8e8f763c251fc0bcc757fb4f6806d09b36f1f7154ea90869651` | `0f8a85f857961e549c89dc24dbe856fb1439197d04757896b568444d00477fd7` | `fde691c59c7a34ed2d80c4013326820f14422b333ce37610706301157787de2b` | `f4e00b92e45b03b186d88ec2ac99f547c36fd384050e9d9fbd43e9e3aa2b9d71` |
| P2 / `RV1` | `687d6c807a8b0d08aa66fc7bee2959341341650c7d8d9af4f45f6ee9ed122967` | `559618ac5d7764cb1c7463e630239df4ba45594b99369039f5f2d02533122153` | `8e8d3f7ce0b30e64c3a721f9786434edffb1634e93e608a0b21193b4863da844` | `9aa73658ce3beff78a83a449fdb6f327ee3b75860aea899a4fb38e9a6a4c3a87` | `5ae21e895ba750dde5402780574463d64984b82108abeb6d7ef7bd757ce85ab6` |
| P3 / `RV1` | `e9e9c7b902d70ab58d193a14880f55948a644fcee52f4a1ade219b508ccfd6b3` | `805e659b096e41b4a7a1f395f875957ff97337d519d6c8ff27b90aef190ca819` | `ba2a8b2b69ed8696241af0ee8ccb26bbbce564335656f9ed0236bf18200b52b8` | `67423511674c9f4b6e6b69eda2c37a722321ab823915c3ed6255f2feaa44669f` | `773982fef54c764e1898915d1ff82afb2db77c9b6a057bd7a5911860c3461764` |

| Candidate | Total tasks | PASS | FAIL | Substantive UNCERTAIN | Routing mismatch | Not applicable | Blocked missing context | Open Findings | Open triage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| P1 | 44 | 6 | 23 | 9 | 2 | 2 | 2 | 32 | 0 |
| P2 | 54 | 7 | 29 | 13 | 0 | 0 | 5 | 42 | 0 |
| P3 | 61 | 32 | 8 | 19 | 0 | 2 | 0 | 27 | 0 |
| **Total** | **159** | **45** | **60** | **41** | **2** | **4** | **7** | **101** | **0** |

Only FAIL and substantive UNCERTAIN results became Findings. The thirteen execution/routing states remain visible in separate human triage queues and all now have decisions. All 101 Findings remain open. The Workbench has not accepted criticism, created a RevisionAction, or claimed that any manuscript was revised. All three projects verify successfully with the exact review snapshots and derived indexes above.

P2 also records an unusable `attempt-0001` rather than silently replacing it. Its response (`ca7ec18332209fc31b55396e6b5010f6c694f1913ce3ce944a7720d784952889`) referenced A2 outside C7's permitted context and was rejected by the validator; its attempt record is `a24fa6193ad1d94b1ce65ae989e35eddd602a4dbf63e63ca9c6da409715efe62`. The corrected output was collected as distinct valid `attempt-0002`, which is the current result shown above.

P3 likewise preserves an unusable `attempt-0001`: twelve proposed PASS results omitted the independent upstream support binding required by their check policies. Its response is `2c3cdac116ed4cf95eb9c45ea3b6dd1755c60095a0c177afad7dc04697e6f267` and its attempt record is `e0d1d3e3cfe9c07a5c05608d90436b853c11f79810676a13b539a9caaecac886`. The corrected `attempt-0002` adds allowed directed paths where evidence exists and changes two unsupported PASS proposals to substantive UNCERTAIN rather than manufacturing support.

### Author status-triage progress

P1's six non-evaluated states have received append-only human decisions: four were acknowledged and two were rejected, leaving no open P1 triage item. The author acknowledged one historical-comparability routing mismatch, one not-applicable stipulative-definition check, and two missing-context statuses requiring source context or independent evidence. The author rejected historical source silence as a routing mismatch and rejected treating the empirical identifiability of the stand-in definition as not applicable; both are marked for substantive rerun rather than silently dropped.

The current P1 triage index is `5d115bfddd36c27c238cf20f050614af51f72f86cb7664f951d0f932f8cf94f1`. Ordered decisions `ST0001`–`ST0006` have hashes `33a49eea7b7c142b7987642eb7ffe896eabc22238197a263bc97c973f3f51e03`, `6fadbcf3510ffab61ee36462496344d04b938ed8a5fb0c104cada0abeecd3177`, `31230bba6e11f74a646944baff06400d6eaeec120bb666bbf948b688a3c983e6`, `3af300f3cdaebefd374ac59fbc884c2ce2aef911b5b8ea3840375a7c1f0c5e1d`, `ad62563886ac5a35363dc231dee5667ace49e9edec4f28e06b6f9e12b688f088`, and `f6d7a4cb4216f2f801ed804cc11ba796d190fe98d9e1d01fdea643dbc4020661`. These decisions govern workflow routing only; none adjudicates a manuscript Finding.

P2's five missing-context states have also received append-only human decisions: four were acknowledged and one was rejected, leaving no open P2 triage item. The author requested added source context for the Marxism interpretation, the Russell-paradox programmes, and the broad historical scope of C12. The author rejected the claim that C6's source-silence check requires a predefined historical source universe and directed the check to make a substantive judgment from the existing material.

The current P2 triage index is `4374e3820bc0ef06eade8e3a633de113606289eb2d6bf57f81e6e584c4f8bb40`. Ordered decisions `ST0001`–`ST0005` have hashes `a600b053353304a4cf80f96976d7e86f1f2b43bc990aaf66ce93e3d61fc534de`, `c2aabf520df8b32b05f747a33b69ad59447c24be4e31e2b7c3095f63fd3f5596`, `64368c3cc1f83e2f4b5403047131c66c73404ac0a42d6c6d5990ed22a58ae550`, `24601bed03bfe401bd20af5228658f1274a4701537b191a5bc369ce31677776d`, and `e9359f16b6db8ef77f67e0a82d6d11aa38dc519f8e8bad8dd4ad8948ef94bb0d`.

P3's two not-applicable states are also decided: the author rejected excluding sampling scrutiny from the single-project C6 description but acknowledged that a comparison denominator is unnecessary because C6 makes no prevalence or population-rate claim. The current P3 triage index is `688525e84c596edd6fd3ff9f2c12f9476da4150b6aaf53fea696942f89c223f7`; `ST0001` and `ST0002` have hashes `1e98e3b76defebae8b10d2d45aa09f56c5a40a2527d0ba060c271de8d4ae24d8` and `049ba834a0be713bdb0f3d91993d5f5ccaa6cbab1d90882f74fc11c0e31991f1`.

Across the three projects, four model-proposed exit states were rejected by the author and carry `rerun_review`: P1 T14 and T19, P2 T22, and P3 T24. Triage completion must not make those checks disappear; each rejected status must receive a later evaluated result before Finding adjudication is treated as current.

Every manuscript must complete the same Phase 1–3 path:

1. in a fresh conversation with no prior context, preserve one direct full-text chat prompt, raw response, provider/model ID, full-manuscript delivery declaration, and actual elapsed time before Workbench Findings influence the evaluator;
2. import the exact source and collect a Raw IR model response;
3. start an `ir-inspection` work session, inspect and correct the IR without editing JSON, then finish that exact session so correction burden has system-timed evidence;
4. prepare a scoped v3 Rule Review and collect at least one current result with relation-bound PASS support paths;
5. acknowledge or reject every non-evaluated execution/routing status through the separate human triage queue;
6. adjudicate every evaluated FAIL/substantive UNCERTAIN Finding as accept, reject, or defer;
7. create the revision plan and verify the whole Workbench project.

Generate the same versioned direct-review protocol for every project with `ir gate-a prepare-baseline PROJECT PROMPT.md`. It embeds the bound source bytes verbatim and refuses to overwrite an existing prompt. Run that exact prompt in a fresh model conversation, then collect it with `ir gate-a baseline PROJECT --prompt-file ... --response-file ... --model-label ... --model-provider ... --model-id ... --interaction-mode fresh-session --prior-context none --manuscript-delivery inline --full-manuscript-confirmed --started-at ... --completed-at ...`. Inline collection and later verification reject a prompt that does not actually contain the exact manuscript bytes. Then run `ir gate-a readiness PROJECT...`. Readiness reports model Findings, Finding adjudications, execution-status triage, baseline control failures, and IR-inspection timing separately without creating an assessment or Gate decision. `ir gate-a init` refuses missing or uncontrolled baselines, duplicate sources, invalid workspaces, missing revision plans, open Findings, open triage items, open work sessions, or a missing pre-review IR-inspection session. The resulting v5 corpus is immutable and binds the exact baseline, triage indexes, and eligible work-session records.

Wrap human activities with `ir gate-a session start PROJECT --activity ir-inspection` and `ir gate-a session finish PROJECT GS1`; use `session list` to inspect open, completed, and abandoned intervals. If the user leaves before making the named judgments or an interval becomes invalid, close it with `ir gate-a session abandon PROJECT GS1 --reason "..."`. The abandonment artifact is immutable and auditable but never enters Gate timing or satisfies the pre-review inspection requirement. The session artifacts use actual system timestamps and enter project verification. Finish IR inspection before collecting the first Rule Review result. Gate v5 binds only exact completed work-session records, derives elapsed milliseconds, and rejects self-reported correction minutes. Historical v1–v4 artifacts remain verifiable without retroactive rewriting.

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
