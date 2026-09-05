# Multi-manuscript divergence calibration

Status: **two additional model campaigns verified; pending independent human blind pairing**

As of 2026-09-05, the maintainer explicitly deferred human-produced artifacts.
The two verified model campaigns remain available, but blind pairing and the
cross-manuscript W/B decision are deferred from current engineering acceptance.

The existing W/B observation on 《结构的替身》 is manuscript-specific. It must
not be generalized to the critics until the same protocol is repeated on at
least two additional finalized manuscripts with materially different subject
matter or style.

## Controlled matrix

Keep executor/model configuration, protocol bytes, repeat count, score margin,
and blind-pairing rules fixed across rows. Use an independent campaign directory
and completed scorecard for every manuscript.

| Corpus | Subject/style | Campaign manifest | Blind review | Completed scorecard | W interval | B interval | Verdict | Reversals/notes |
|---|---|---|---|---|---:|---:|---|---|
| M1 · 结构的替身 | social-theory essay | pending archival import | pending | pending | pending | pending | pending | existing narrative observation is not a scorecard substitute |
| M2 · modernity-epistemology-bacon-to-kant.md | intellectual-history / explanatory essay | `evidence/divergence-campaigns/modernity/20260830T175953.404338Z--campaign/campaign.json` | package ready; human pairing pending | pending | pending | pending | pending | source SHA-256 `DF21111765E7AAD646A03A3A70668826542BA935029F2DDFE32B3FB597731FBB`; campaign verified |
| M3 · tokenized-gold-collateral-layer.md | market-structure / financial analysis | `evidence/divergence-campaigns/tokenized-gold/20260830T174200.766770Z--campaign/campaign.json` | package ready; human pairing pending | pending | pending | pending | pending | source SHA-256 `C5BEB8F9C957C0E614754F55EAE902949DE91B7AA9B2C0F35582DB31C5001276`; campaign verified |

The two additional candidates were selected from finalized local manuscripts on
2026-08-31. Their different subject matter and explanatory versus market-analysis
styles make them a stronger dependency check than two essays from the same
philosophical series. Recheck each source hash before starting a campaign; a hash
change means the corpus must be reviewed and identified as a new version.

## Fixed execution plan

For both new manuscripts, hold these variables constant:

- protocols: `critic-individualist`, `critic-contrastivist`;
- repetitions: two per protocol, serial and counterbalanced;
- executor: `gpt-5.6-luna`, read-only sandbox, `low` reasoning setting;
- timeout: 900 seconds per run;
- separate deterministic order seeds: `p3-modernity-v2-low` and
  `p3-tokenized-gold-v2-low`;
- human blind pairing only; the executor, maintainer, and manuscript author may
  not fill the pairing judgments.

The exact candidate bytes were confirmed clean, committed, and anonymously
available from the public `UYMIDGameStudio/billcharles-blog` GitHub remote before
model execution. An earlier launch was rejected before process start while the
publication status was unknown. A later default-reasoning tokenized-gold campaign
is preserved as failed evidence because only one run succeeded before the
executor hit its usage limit. It is not mixed into the successful campaign.

## Verified model evidence

Both successful campaigns were independently checked with `verify-campaign`
against the original source. Every campaign contains four structurally valid
reports, a traceable scorecard template, and a separately generated de-identified
review artifact.

| Corpus | Campaign JSON SHA-256 | Scorecard template SHA-256 | Blind review SHA-256 | Private key SHA-256 |
|---|---|---|---|---|
| M2 | `B3633D68CA1BFC6F4D8DCDD800E5C496F509E7018BFE15B8D0B543DC845ED3DB` | `F4F00B45D53F7FCD1E3F9C30BA3AB4D56567E8A63389966006B627F9CB33FA48` | `02BB9257D40051A5DADA57C1B3A3A2B261E7B120A5D6EE9E5151302BC11117C4` | `5750D6CBE7C798DAFB04FB27B150DF7D6EF71868C7AA986C5479A870C88F63BC` |
| M3 | `DF86AD9C19AD95867EBD12B19050B8E5459CC35B4A54A095B946C89CB0EDBDA2` | `5651B55C93174F96A8CED896F2A1618B00F3A352E2E6CCC96A3E0B505DF123D6` | `6DDE9AAC36F31E4C1C1098C62C7620499839ED5CDB402CFE09A02628B13CF7CB` | `A268660A0DB866B71CD7E55CE4027D71EBB9B5E31951751D7DF86C95C53659E9` |

The private keys must remain withheld from the pairer. The W/B cells above must
remain pending until a qualifying human returns both completed blind artifacts;
model self-pairing or maintainer pairing would invalidate the comparison.

For each row:

1. run two isolated repetitions of each critic with `campaign`;
2. validate all four reports and preserve their exact hashes;
3. use `blind-scorecard`; give only the de-identified artifact to the human
   pairer and record any suspected unblinding from writing style;
4. apply the completed blind scorecard and generate Markdown with `score`;
5. record weakest-step stability, strongest-surviving-argument stability,
   framework reversals, and unverified citations outside the W/B arithmetic.

## Cross-manuscript decision

After all rows verify, compare the interval verdicts rather than averaging them
into one headline number. If the verdict or the B−W separation changes across
manuscripts, document that dependency explicitly and restrict claims to the
tested manuscript classes. P3-2 is complete only after two or three distinct
manuscripts have verified campaigns and human-completed pairings; an empty
matrix, generated template, or model self-pairing is not evidence.
