# Phase 7 real Citation provenance demo

On 2026-08-23 the author completed one substantive Citation-verification
vertical slice against an adopted V2 manuscript. Manuscript bytes and local
workspace locators remain private; this record publishes only the Citation
identity, external source, outcomes, exact artifact hashes, and limitations.

## Audited Citation

- Argument IR Citation: `V2:Z1`
- Work: W. V. O. Quine, “Two Dogmas of Empiricism”
- External source: [University of Zurich-hosted 1951/1961 comparative text](https://www.theologie.uzh.ch/dam/jcr:ffffffff-fbd6-1538-0000-000070cf64bc/Quine51.pdf)
- Citation relationship: Citation → Evidence → Claim

The source identifies the original *Philosophical Review* publication and its
later reprint history. Its opening rejects a fundamental analytic/synthetic
cleavage; later sections develop whole-system confirmation, a field or fabric
of beliefs, revisability, and rejection of an absolute boundary. This supports
the manuscript's compact characterization. “Web of belief” is treated as an
interpretive label, not as Quine's exact phrase in this text.

## Four-dimension outcome

| Dimension | Model proposal | Human final |
|---|---|---|
| Bibliographic existence | `verified` | `verified` |
| Exact source located | `verified` | `verified` |
| Content supports wording | `supports` | `supports` |
| Context preserved | `yes` | `yes` |

Before human action, the all-positive model result still produced
`verification_state=unverified`, and the dependent Evidence and Claim remained
`depends_on_unverified_evidence`. The author explicitly confirmed the proposal.
Only then did the deterministic index change both dependencies to
`citation_verified`. No `claim_false` field or manuscript score exists.

## Exact private-artifact bindings

| Artifact | SHA-256 |
|---|---|
| `citation-audit-context` | `2b8bf869d07d0ef28c7d8a4ee65b91445832df3940c4fc814388503668c4ec68` |
| `citation-audit-run` | `c50cc76f741deaa1397de21eaa74c39ea1a4866e2ff3ab99bd01c46455bcd64e` |
| Raw model result | `9ad451aaca8780e979a03970375fc3ab15d2e522c1d870110ced64b95b2b212d` |
| Result-attempt record | `561720eeb962e7fe0e883d52c8198c5b3d97ba5adcc176d2b0fec8a46cecbd38` |
| Human decision `CD0001` | `0f11399123155395d9a226d57ad98dab3c157530b06f57254c790de1c17d8986` |
| Citation provenance index | `bd4cc9cf5c8fa36b02572944812c22d7c87e3c704f276c9af4687515d3c68279` |
| Readable evidence report | `79b23d77c12969097900ee48cf7345507d8e5ab547e4759ad89b58477bfed2b8` |

The complete private project passes `ir verify-project` after the decision.

## Remaining limitations

- This is one real Citation, not a comprehensive audit of either manuscript.
- Exact quotations or page citations should identify whether the 1951 article
  or the 1961 revised reprint is being used.
- Composite IR Citation nodes that summarize several unrelated statistics
  should be split through normal IR correction before substantive verification;
  the verifier must not manufacture one source that appears to cover all of
  them.
- Source retrieval remains provider-neutral and external to the Python engine.
  The engine validates sources, results, decisions, hashes, and dependencies; it
  does not claim that successful HTTP access proves semantic support.
