# Phase 4 real-manuscript Perspective Lens demo

This demo was executed on the author-owned manuscript 《结构的替身》 after the human-confirmed Product Gate A decision. The private manuscript and Workbench remain outside this repository. The demo records only Claim text, artifact identities, hashes, and model-derived outcomes; it does not bundle private source bytes.

## Target

The run deliberately selected one known framework-reversal anchor rather than reviewing every Claim:

- `V1:C7`: 替身不仅承接愤怒，也让难以明说的结构判断进入公共认知
- Scope: `claim`
- Current Reviewed IR SHA-256: `8d01bea49786f872216fdbcc215143c8aabad0b418c6c9a79b2ea69d26459ebb`
- Producer label: `gpt-5.6-sol-high`

The Product Gate A workspace was first copied into a separate Phase 4 continuation workspace. This matters because the Gate corpus binds the exact earlier revision-plan bytes: adding a new Perspective Finding to the captured workspace would correctly make that historical locator drift. The original Gate workspace was restored and the Gate corpus again verifies byte-for-byte; the Phase 4 continuation workspace also verifies independently.

## Lens outcomes

The existing Social Science Rule Lens had four current outcomes for C7: two general failures, one uncertain causal-mechanism check, and one alternative-explanation failure. Phase 4 then added two complete Perspective Lens runs.

### Methodological individualism — FAIL

The model-derived judgment says that “认知管道” names an aggregate transition without reconstructing which actors encounter the person-centered discourse, how their beliefs change, or how those changes aggregate into public cognition. It proposes narrowing C7 to an interpretive claim or adding an actor-level transmission mechanism and evidence.

- Review: `PV1 / attempt-0001`
- Plan SHA-256: `70bbea47e1bb6a2762b03eb98a07594c7634a27d5c89f82f68ec7a60f9163361`
- Raw result SHA-256: `42cd24bb62afed3bd5cc92b5645a605ce35ffde8c97d8d3f4da28b883d99d14c`
- Index SHA-256: `2a48cc87f5e3f19ae3023031c9100090148202262fddeb93ee1e5d5a2768077b`
- Finding: `V1-PV1-attempt-0001-F0001`
- Finding SHA-256: `158067edf89ff7b53a417e417fbb80fe7d84507996837c62e746ced59978b249`

### Contrastive explanation — PASS

The model-derived judgment says C7 states a relevant contrast: a structural judgment that is difficult to articulate directly becomes sayable and thinkable through a concrete person. C8 and C9 preserve the alternative downstream tendencies rather than collapsing them into a single beneficial effect. This PASS does not answer the Individualist or Social Science objections.

- Review: `PV2 / attempt-0001`
- Plan SHA-256: `74cd710dd1c599c609ea625ec9909cd45c5afb1cb0867e201c142705ef8fa069`
- Raw result SHA-256: `16fc87dca3c480b57c3440d7c79e225907556d99ce96768be9d43cf4442677a1`
- Index SHA-256: `2e25c71ea718d8ac455246d81186fefd4c8933bec770391cd6695b0ab9d7b990`
- Finding: none; PASS remains visible without creating an actionable Finding

## What the demo establishes

`ir review show-claim-lenses ... --claim C7` displays the Rule Lens and both Perspective Lenses in separate sections. It computes no confidence percentage, majority, winner, or synthesis. The Individualist FAIL becomes a normal open `argument-finding` eligible for the existing human adjudication workflow; the Contrastivist PASS remains an auditable model result but does not create a task.

The run also exposed and fixed a cache lifecycle defect: collecting a new valid result after an existing revision plan made that plan stale. Both Rule and Perspective result collection now rebuild the deterministic adjudication/revision-plan projection after deriving new Findings, while leaving all immutable inputs and human decisions untouched.

This is a vertical-slice demonstration, not evidence that either framework judgment is true. The new Individualist Finding remains open until the author accepts, rejects, or defers it.
