# Workbench Phase 1 demo fixture

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
