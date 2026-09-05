# P3 human evidence handoff

Status: **ready for external participants; no human result has been received**

2026-09-05: human participation is deferred at the maintainer's request. Retain
this handoff for future use; it does not block the current code-review work.

This handoff keeps the two remaining human roles separate. A person may act as
the divergence pairer or as an external Gate participant, but should not do both
for the same evidence collection. The maintainer must not fill missing human
judgments, infer them from prose, or convert an invitation into a result.

## Role 1: blind divergence pairer

Send the pairer only these two files, not their containing directories:

1. `evidence/divergence-campaigns/modernity/20260830T175953.404338Z--campaign/blind-review.json`
2. `evidence/divergence-campaigns/tokenized-gold/20260830T174200.766770Z--campaign/blind-review.json`

Do not send `blind-key.json`, `campaign.json`, `scorecard.json`, run directories,
protocol names, or executor logs until the completed judgments have been applied.
The pairer must work from the de-identified claims only, pair claims one-to-one,
set each classification to `overlap`, `different_reason`, or `ambiguous`, and set
`complete` to `true`. They must not edit the supplied runs, claim text, IDs, or
`key_id`. Record whether the pairer suspected either critic's identity from its
writing style; suspicion is a limitation, not a reason to reveal the key.

Have the pairer return two clearly named files, for example:

- `modernity-blind-review-completed.json`
- `tokenized-gold-blind-review-completed.json`

The coordinator then applies each result without overwriting the template:

```text
python critic_runner.py apply-blind-scorecard <campaign>/scorecard.json <completed-blind.json> --key <campaign>/blind-key.json --output <campaign>/completed-scorecard.json
python critic_runner.py score <campaign>/completed-scorecard.json --format markdown --output <campaign>/score.md
python critic_runner.py verify-campaign <campaign> --source <original-manuscript>
```

After both score files exist, update
`docs/divergence-multi-manuscript-calibration.md` with the W/B intervals, verdicts,
framework reversals, and any manuscript dependency. Do not average the manuscripts
into a single critic score.

## Role 2: external Gate participant

The participant must be a real writer or editor who did not author this product
or the existing author-owned Gate evidence. They must use manuscripts they own
or are authorized to edit and choose Gate A or Gate B based on their real corpus:

- Gate A requires 3-5 completed real-manuscript Workbench projects and records
  Phase 1-3 usability, IR correction work, and a direct-review comparison.
- Gate B requires 2-3 completed real multi-version projects with human lineage
  and Finding Resolution decisions.

Before starting, assign a non-identifying participant alias and record consent
scope. The participant uses the normal Workbench/UI and CLI only; they must not
hand-edit JSON. The facilitator may explain installation or command syntax, but
must log every request for help and must not choose assessments or the final
`pass`, `fail`, or `defer` decision.

Use the CLI's own read-only readiness output as the checklist:

```text
python critic_runner.py ir gate-a readiness <project-1> <project-2> <project-3>
python critic_runner.py ir gate-a init <new-gate-directory> <project-1> <project-2> <project-3>
```

or, for an already qualifying multi-version corpus:

```text
python critic_runner.py ir gate-b init <new-gate-directory> <project-1> <project-2>
```

Run `assess`, `report`, `decide`, and `verify` through the corresponding Gate
subcommands. Use `--help` for the required human fields instead of editing an
artifact directly. Preserve the final verified directory under the participant
alias, then add only consented metadata and hashes to
`docs/external-product-gate-validation.md`. Keep manuscript bytes private unless
the participant separately authorizes publication.

## Completion boundary

The two verified model campaigns are not P3-2 completion without returned human
blind pairings. This handoff is not P3-1 completion without at least one verified
external Gate assessment and decision. Until those artifacts exist, both items
remain pending.
