# Workbench Phase 1-3 demo fixture

This is a small realistic Chinese argument structure created for regression and UX demonstrations. It is not the text of 《结构的替身》 and is not treated as gold truth.

The supplied Raw IR is structurally valid but deliberately debatable:

- C1's “总会” formulation is classified only as descriptive even though it also carries a mechanism/causal commitment.
- R1 attaches case evidence E1 to the conceptual definition C2 instead of the empirical generalization C1.
- The opening observation that controversy begins with institutions is omitted as a Claim.
- The Girard citation is extracted but substantively unverified.

Run:

```powershell
py -3 critic_runner.py ir init .\test\fixtures\workbench-demo\manuscript.md --project-dir .\demo.argument-workbench
py -3 critic_runner.py ir collect .\demo.argument-workbench --file .\test\fixtures\workbench-demo\raw-ir.json --producer-label fixture-model
py -3 critic_runner.py ir inspect .\demo.argument-workbench
```

A demonstration correction session can:

1. Edit C1 `types`, `methods`, and `uncertainty` to record the user's interpretation of “总会”.
2. Edit R1's `to` endpoint from C2 to C1.
3. Add the omitted opening Claim using its exact source quote and bind it to C3.
4. Inspect the Citation without claiming that bibliographic extraction verifies its content.
5. Undo any one correction and observe that a new revert event is appended.

The resulting `argument-map.md` should make the model-derived and human-confirmed fields visible without requiring the user to open any JSON file.

To demonstrate Phase 2 without editing the IR first, initialize a fresh project and continue with the bundled plan-bound result:

```powershell
py -3 critic_runner.py ir review prepare .\demo.argument-workbench --depth core
py -3 critic_runner.py ir review collect .\demo.argument-workbench --file .\test\fixtures\workbench-demo\review-results.json --producer-label fixture-review-model
py -3 critic_runner.py ir review show .\demo.argument-workbench --claim C1
py -3 critic_runner.py ir verify-project .\demo.argument-workbench
```

The v3 result deliberately produces a denominator Finding for C1's universal wording and a substantive uncertain rival-reading Finding for C3. Every PASS that requires upstream support names a node other than the target Claim in `support_refs` and supplies a directed `support_paths` relation chain; the citation-policy check starts from Z1 through `cites`. This fixture is a regression anchor, not a claim that either model judgment is human-confirmed.

Continue into the Phase 3 human workflow:

```powershell
py -3 critic_runner.py ir adjudicate .\demo.argument-workbench
py -3 critic_runner.py ir revision-plan .\demo.argument-workbench --show
py -3 critic_runner.py ir verify-project .\demo.argument-workbench
```

One realistic session accepts C1's denominator Finding with `narrow_claim` or `add_evidence`, and defers C3's rival-reading Finding with a recorded reason. Those are demonstration choices, not bundled model decisions or gold adjudications. Each confirmation is append-only and can be reconsidered later without deleting its history.

Phase 7 can be demonstrated without pretending that the fixture Citation has
been verified. Start from a fresh, uncorrected fixture workspace so the bundled
context hashes match:

```powershell
py -3 critic_runner.py ir citations prepare .\demo.argument-workbench
py -3 critic_runner.py ir citations collect .\demo.argument-workbench `
  --file .\test\fixtures\workbench-demo\citation-audit-results.json `
  --producer-label offline-regression
py -3 critic_runner.py ir citations show .\demo.argument-workbench
py -3 critic_runner.py ir citations decide .\demo.argument-workbench `
  --citation Z1 --decision confirm `
  --reason "确认当前四个维度都仍未核实，不把模型记忆当来源"
py -3 critic_runner.py ir verify-project .\demo.argument-workbench
```

The result intentionally has no sources and keeps all four dimensions
`uncertain`. Before the human decision, even a positive model result would
remain a proposal. After this particular confirmation, Z1 remains unverified
and the Claims reached through `Z1 --cites→ C2 --qualifies→ C1 --supports→ C3`
are displayed as `depends_on_unverified_evidence`, never `claim_false`.
