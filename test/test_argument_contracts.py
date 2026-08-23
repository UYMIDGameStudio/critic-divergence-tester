from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_contracts as contracts  # noqa: E402


STAMP = "2026-08-08T00:00:00+00:00"


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def base(
    artifact: str,
    artifact_id: str,
    lifecycle: str,
    origin: str,
    *,
    parents: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifact": artifact,
        "artifact_id": artifact_id,
        "lifecycle": lifecycle,
        "provenance": {
            "origin": origin,
            "created_at": STAMP,
            "producer": "test",
        },
        "parents": parents or [],
    }


def parent(role: str, artifact: str, value: object) -> dict[str, str]:
    return {
        "role": role,
        "artifact": artifact,
        "sha256": contracts.sha256_bytes(encoded(value)),
    }


class ArgumentContractTests(unittest.TestCase):
    def test_project_document_version_contracts_are_strict(self) -> None:
        project = {
            **base("argument-project", "P1", "immutable", "human-confirmed"),
            "project_id": "P1",
            "title": "结构的替身",
        }
        document = {
            **base(
                "argument-document",
                "D1",
                "immutable",
                "human-confirmed",
                parents=[parent("project", "argument-project", project)],
            ),
            "project_id": "P1",
            "document_id": "D1",
            "title": "结构的替身",
        }
        version = {
            **base(
                "document-version",
                "V1",
                "immutable",
                "human-confirmed",
                parents=[parent("document", "argument-document", document)],
            ),
            "project_id": "P1",
            "document_id": "D1",
            "version_id": "V1",
            "source": {
                "name": "draft.md",
                "relative_path": "source/draft.md",
                "sha256": "a" * 64,
            },
            "parent_version": None,
        }
        self.assertEqual(contracts.validate_project(project), [])
        self.assertEqual(contracts.validate_document(document), [])
        self.assertEqual(contracts.validate_document_version(version), [])
        self.assertEqual(
            contracts.validate_contract_bundle(
                [(project, encoded(project)), (document, encoded(document)), (version, encoded(version))]
            ),
            [],
        )

        drift = copy.deepcopy(version)
        drift["quality_score"] = 91
        self.assertTrue(any("exactly" in error for error in contracts.validate_document_version(drift)))
        escaped = copy.deepcopy(version)
        escaped["source"]["relative_path"] = "../../outside.md"
        self.assertTrue(
            any("inside" in error for error in contracts.validate_document_version(escaped))
        )
        wrong_origin = copy.deepcopy(project)
        wrong_origin["provenance"]["origin"] = "model-derived"
        self.assertTrue(
            any("human-confirmed" in error for error in contracts.validate_project(wrong_origin))
        )

    def test_lineage_supports_split_merge_new_and_removed(self) -> None:
        def lineage(
            relation: str, from_claims: list[str], to_claims: list[str]
        ) -> dict[str, object]:
            lineage_parents: list[dict[str, str]] = []
            if relation != "new":
                lineage_parents.append(
                    {"role": "from-version", "artifact": "argument-ir", "sha256": "3" * 64}
                )
            if relation != "removed":
                lineage_parents.append(
                    {"role": "to-version", "artifact": "argument-ir", "sha256": "4" * 64}
                )
            return {
                **base(
                    "claim-lineage",
                    "L1",
                    "immutable",
                    "model-derived",
                    parents=lineage_parents,
                ),
                "lineage_id": "L1",
                "from_claims": from_claims,
                "to_claims": to_claims,
                "relation": relation,
                "proposed_by": "model",
                "proposal_sha256": None,
                "status": "proposed",
            }

        self.assertEqual(
            contracts.validate_claim_lineage(lineage("split", ["V1:C4"], ["V2:C7", "V2:C8"])),
            [],
        )
        self.assertEqual(
            contracts.validate_claim_lineage(lineage("merged", ["V1:C4", "V1:C5"], ["V2:C7"])),
            [],
        )
        self.assertEqual(
            contracts.validate_claim_lineage(lineage("new", [], ["V2:C11"])),
            [],
        )
        self.assertEqual(
            contracts.validate_claim_lineage(lineage("removed", ["V1:C8"], [])),
            [],
        )
        bad = lineage("split", ["V1:C4", "V1:C5"], ["V2:C7"])
        self.assertTrue(any("split" in error for error in contracts.validate_claim_lineage(bad)))

        confirmed = lineage("modified", ["V1:C4"], ["V2:C7"])
        confirmed["status"] = "human_confirmed"
        self.assertTrue(any("human-confirmed" in error for error in contracts.validate_claim_lineage(confirmed)))

        proposal = lineage("modified", ["V1:C4"], ["V2:C7"])
        confirmation = copy.deepcopy(proposal)
        confirmation["artifact_id"] = "L1-confirmation"
        confirmation["status"] = "human_confirmed"
        confirmation["provenance"]["origin"] = "human-confirmed"
        confirmation["proposal_sha256"] = contracts.sha256_bytes(encoded(proposal))
        confirmation["parents"] = list(proposal["parents"]) + [
            parent("proposal", "claim-lineage", proposal)
        ]
        self.assertEqual(contracts.validate_claim_lineage(confirmation), [])

    def test_accept_requires_revision_action_in_bundle(self) -> None:
        finding = {
            **base(
                "argument-finding",
                "F1",
                "immutable",
                "model-derived",
                parents=[
                    {"role": "target-ir", "artifact": "argument-ir", "sha256": "1" * 64},
                    {
                        "role": "lens-result",
                        "artifact": "argument-check-results",
                        "sha256": "2" * 64,
                    },
                ],
            ),
            "finding_id": "F1",
            "target_claim": "V1:C4",
            "lens": {"kind": "rule", "id": "social-science", "check_id": "descriptive.denominator"},
            "verdict": "fail",
            "reason": "三个案例都是正例，没有比较分母。",
            "evidence_refs": ["V1:C4", "V1:E3"],
            "status": "open",
        }
        adjudication = {
            **base(
                "finding-adjudication",
                "AD1",
                "immutable",
                "human-confirmed",
                parents=[parent("finding", "argument-finding", finding)],
            ),
            "adjudication_id": "AD1",
            "finding_id": "F1",
            "decision": "accept",
            "reason": "",
            "supersedes": None,
        }
        entries = [(finding, encoded(finding)), (adjudication, encoded(adjudication))]
        self.assertTrue(
            any("requires at least one revision-action" in error for error in contracts.validate_contract_bundle(entries))
        )
        action = {
            **base(
                "revision-action",
                "RA1",
                "immutable",
                "human-confirmed",
                parents=[parent("adjudication", "finding-adjudication", adjudication)],
            ),
            "action_id": "RA1",
            "adjudication_id": "AD1",
            "target_claim": "V1:C4",
            "action_type": "narrow_claim",
            "text": "把‘总会’收窄为当前三个案例中的观察。",
        }
        self.assertEqual(
            contracts.validate_contract_bundle(entries + [(action, encoded(action))]),
            [],
        )

    def test_finding_resolution_requires_original_lens_retest_and_human_confirmation(self) -> None:
        bound = lambda name, digit: {"relative_path": name, "sha256": digit * 64}
        run = {
            **base(
                "resolution-retest-run", "RR1", "immutable", "deterministic",
                parents=[
                    {"role": "original-finding", "artifact": "argument-finding", "sha256": "1" * 64},
                    {"role": "accepted-adjudication", "artifact": "finding-adjudication", "sha256": "2" * 64},
                    {"role": "revision-action-0001", "artifact": "revision-action", "sha256": "3" * 64},
                    {"role": "confirmed-lineage", "artifact": "claim-lineage", "sha256": "4" * 64},
                    {"role": "target-ir", "artifact": "argument-ir", "sha256": "5" * 64},
                    {"role": "lens-protocol", "artifact": "argument-check-library", "sha256": "6" * 64},
                ],
            ),
            "resolution_id": "RR1", "document_id": "D1",
            "from_version": "V1", "to_version": "V2",
            "original_finding_id": "F1", "descendant_claims": ["V2:C7"],
            "lens": {"kind": "rule", "id": "social-science", "check_id": "descriptive.denominator"},
            "original_finding": bound("original-finding.json", "1"),
            "accepted_adjudication": bound("accepted-adjudication.json", "2"),
            "revision_actions": [bound("revision-actions/RA1.json", "3")],
            "confirmed_lineage": bound("confirmed-lineage.json", "4"),
            "target_ir": bound("target-argument-ir.json", "5"),
            "lens_protocol": bound("lens-protocol.json", "6"),
            "lens_content": bound("lens-content.txt", "b"),
            "prompt": bound("resolution-retest-prompt.md", "7"),
        }
        self.assertEqual(contracts.validate_resolution_retest_run(run), [])
        no_action = copy.deepcopy(run)
        no_action["revision_actions"] = []
        no_action["parents"] = [parent for parent in no_action["parents"] if parent["role"] != "revision-action-0001"]
        self.assertTrue(any("non-empty" in error for error in contracts.validate_resolution_retest_run(no_action)))

        results = {
            "schema_version": 1, "artifact": "resolution-retest-results",
            "source": {"original_finding_sha256": "1" * 64, "target_ir_sha256": "5" * 64, "lens_protocol_sha256": "6" * 64},
            "status": "complete", "unverified": [],
            "results": [{"target_claim": "V2:C7", "verdict": "pass", "reason": "A denominator was added.", "basis_refs": ["V2:C7", "V2:E4"], "support_refs": ["V2:E4"], "support_paths": [{"support_ref": "V2:E4", "relation_ids": ["V2:R2"]}], "analysis": "The original check now passes."}],
        }
        self.assertEqual(contracts.validate_resolution_retest_results(results), [])
        proposal = {
            **base(
                "finding-resolution-proposal", "RP1", "derived-replaceable", "deterministic",
                parents=[
                    {"role": "retest-run", "artifact": "resolution-retest-run", "sha256": "8" * 64},
                    {"role": "result-attempt", "artifact": "resolution-result-attempt", "sha256": "9" * 64},
                    {"role": "retest-results", "artifact": "resolution-retest-results", "sha256": "a" * 64},
                ],
            ),
            "resolution_id": "RR1", "original_finding_id": "F1",
            "descendant_claims": ["V2:C7"], "proposed_status": "resolved",
            "mapping_reason": "Every descendant passed the original Lens.",
            "retest_summary": {"pass": 1, "fail": 0, "uncertain": 0},
            "field_provenance": {
                key: {"origin": "deterministic", "source": "resolution status mapping v1"}
                for key in ("retest_summary", "proposed_status", "mapping_reason")
            },
        }
        self.assertEqual(contracts.validate_finding_resolution_proposal(proposal), [])
        decision = {
            **base(
                "finding-resolution-decision", "RD1", "immutable", "human-confirmed",
                parents=[parent("resolution-proposal", "finding-resolution-proposal", proposal)],
            ),
            "decision_id": "RD1", "resolution_id": "RR1", "decision": "confirm",
            "final_status": "resolved", "reason": "The original denominator issue is fixed.",
            "supersedes": None,
        }
        self.assertEqual(contracts.validate_finding_resolution_decision(decision), [])
        forged = copy.deepcopy(decision)
        forged["provenance"]["origin"] = "model-derived"
        self.assertTrue(any("human-confirmed" in error for error in contracts.validate_finding_resolution_decision(forged)))

    def test_rule_review_contracts_separate_model_outcomes_from_derived_index(self) -> None:
        review = {
            **base(
                "rule-review-run",
                "RV1",
                "immutable",
                "deterministic",
                parents=[
                    {
                        "role": "reviewed-ir",
                        "artifact": "reviewed-argument-ir",
                        "sha256": "1" * 64,
                    },
                    {
                        "role": "target-ir",
                        "artifact": "argument-ir",
                        "sha256": "2" * 64,
                    },
                    {
                        "role": "check-library",
                        "artifact": "argument-check-library",
                        "sha256": "3" * 64,
                    },
                ],
            ),
            "review_id": "RV1",
            "project_id": "P1",
            "document_id": "D1",
            "version_id": "V1",
            "lens": {
                "kind": "rule",
                "id": "social-science",
                "library_sha256": "3" * 64,
            },
            "depth": "core",
            "reviewed_ir_record": {
                "relative_path": "reviewed-ir-record.json",
                "sha256": "1" * 64,
            },
            "target_ir": {
                "relative_path": "target-argument-ir.json",
                "sha256": "2" * 64,
            },
            "check_library": {
                "relative_path": "check-library.json",
                "sha256": "3" * 64,
            },
            "plan": {"relative_path": "check-plan.json", "sha256": "4" * 64},
            "prompt": {"relative_path": "review-prompt.md", "sha256": "5" * 64},
        }
        self.assertEqual(contracts.validate_rule_review_run(review), [])
        escaped = copy.deepcopy(review)
        escaped["plan"]["relative_path"] = "../plan.json"
        self.assertTrue(
            any("inside" in error for error in contracts.validate_rule_review_run(escaped))
        )

        attempt = {
            **base(
                "review-result-attempt",
                "RV1-attempt-0001",
                "immutable",
                "model-derived",
                parents=[parent("review-run", "rule-review-run", review)],
            ),
            "review_id": "RV1",
            "attempt_id": "attempt-0001",
            "collection": {
                "method": "file",
                "source_name": "results.json",
                "producer_label": "test-model",
            },
            "response": {"relative_path": "response.json", "sha256": "6" * 64},
            "validation": {"status": "valid", "errors": []},
        }
        self.assertEqual(contracts.validate_review_result_attempt(attempt), [])
        forged_attempt = copy.deepcopy(attempt)
        forged_attempt["validation"]["errors"] = ["ignored model error"]
        self.assertTrue(
            any(
                "empty" in error
                for error in contracts.validate_review_result_attempt(forged_attempt)
            )
        )

        finding = {
            **base(
                "argument-finding",
                "V1-RV1-attempt-0001-F0001",
                "immutable",
                "model-derived",
                parents=[
                    {
                        "role": "target-ir",
                        "artifact": "argument-ir",
                        "sha256": "2" * 64,
                    },
                    {
                        "role": "lens-result",
                        "artifact": "argument-check-results",
                        "sha256": "6" * 64,
                    },
                ],
            ),
            "finding_id": "V1-RV1-attempt-0001-F0001",
            "target_claim": "V1:C1",
            "lens": {
                "kind": "rule",
                "id": "social-science",
                "check_id": "descriptive.denominator",
            },
            "verdict": "fail",
            "reason": "No comparison denominator.",
            "evidence_refs": ["V1:C1"],
            "status": "open",
        }
        index = {
            **base(
                "claim-review-index",
                "RV1-attempt-0001-claim-review",
                "derived-replaceable",
                "deterministic",
                parents=[
                    parent("review-run", "rule-review-run", review),
                    parent("result-attempt", "review-result-attempt", attempt),
                    {
                        "role": "lens-result",
                        "artifact": "argument-check-results",
                        "sha256": "6" * 64,
                    },
                    parent("finding-0001", "argument-finding", finding),
                ],
            ),
            "review_id": "RV1",
            "attempt_id": "attempt-0001",
            "version_id": "V1",
            "lens": review["lens"],
            "summary": {"pass": 1, "fail": 1, "uncertain": 0},
            "outcomes": [
                {
                    "task_id": "T1",
                    "target_claim": "V1:C1",
                    "check_id": "descriptive.denominator",
                    "verdict": "fail",
                    "reason": "No comparison denominator.",
                    "consequence": "The scope must be narrowed.",
                    "evidence_refs": ["V1:C1"],
                    "finding_id": finding["finding_id"],
                },
                {
                    "task_id": "T2",
                    "target_claim": "V1:C1",
                    "check_id": "descriptive.measurement-validity",
                    "verdict": "pass",
                    "reason": "The measure is defined.",
                    "consequence": "",
                    "evidence_refs": ["V1:C1"],
                    "finding_id": None,
                },
            ],
            "view": {"relative_path": "claim-review.md", "sha256": "7" * 64},
            "field_provenance": {
                "outcomes.task_id": {"origin": "deterministic", "source": "plan"},
                "outcomes.target_claim": {"origin": "deterministic", "source": "plan"},
                "outcomes.check_id": {"origin": "deterministic", "source": "plan"},
                "outcomes.verdict": {"origin": "model-derived", "source": "result"},
                "outcomes.reason": {"origin": "model-derived", "source": "result"},
                "outcomes.consequence": {"origin": "model-derived", "source": "result"},
                "outcomes.evidence_refs": {"origin": "model-derived", "source": "result"},
                "outcomes.finding_id": {"origin": "deterministic", "source": "review"},
                "summary": {"origin": "deterministic", "source": "review"},
                "view": {"origin": "deterministic", "source": "review"},
            },
        }
        self.assertEqual(contracts.validate_claim_review_index(index), [])
        false_summary = copy.deepcopy(index)
        false_summary["summary"]["pass"] = 2
        self.assertTrue(
            any(
                "equal outcomes" in error
                for error in contracts.validate_claim_review_index(false_summary)
            )
        )
        fake_pass_finding = copy.deepcopy(index)
        fake_pass_finding["outcomes"][1]["finding_id"] = "F-for-pass"
        self.assertTrue(
            any(
                "null for pass" in error
                for error in contracts.validate_claim_review_index(fake_pass_finding)
            )
        )
        fake_deterministic_verdict = copy.deepcopy(index)
        fake_deterministic_verdict["field_provenance"]["outcomes.verdict"][
            "origin"
        ] = "deterministic"
        self.assertTrue(
            any(
                "must remain model-derived" in error
                for error in contracts.validate_claim_review_index(
                    fake_deterministic_verdict
                )
            )
        )

    def test_perspective_lens_contracts_preserve_framework_and_normalize_findings(self) -> None:
        protocol = {
            **base(
                "perspective-lens-protocol",
                "methodological-individualism-v1",
                "immutable",
                "deterministic",
            ),
            "lens": {"kind": "perspective", "id": "methodological-individualism"},
            "legacy_protocol": "critic-individualist",
            "protocol": {
                "relative_path": "critic-individualist.md",
                "sha256": "3" * 64,
            },
        }
        self.assertEqual(contracts.validate_perspective_lens_protocol(protocol), [])

        plan = {
            **base(
                "perspective-review-plan",
                "PV1-plan",
                "immutable",
                "deterministic",
                parents=[
                    {
                        "role": "target-ir",
                        "artifact": "argument-ir",
                        "sha256": "2" * 64,
                    },
                    parent("protocol", "perspective-lens-protocol", protocol),
                ],
            ),
            "review_id": "PV1",
            "lens": {
                "kind": "perspective",
                "id": "methodological-individualism",
                "protocol_sha256": "3" * 64,
            },
            "review_scope": {
                "kind": "thesis-chain",
                "claim_ids": [],
                "selected_claim_ids": ["C1"],
            },
        }
        self.assertEqual(contracts.validate_perspective_review_plan(plan), [])

        review = {
            **base(
                "perspective-review-run",
                "PV1",
                "immutable",
                "deterministic",
                parents=[
                    {
                        "role": "reviewed-ir",
                        "artifact": "reviewed-argument-ir",
                        "sha256": "1" * 64,
                    },
                    {
                        "role": "target-ir",
                        "artifact": "argument-ir",
                        "sha256": "2" * 64,
                    },
                    parent("protocol", "perspective-lens-protocol", protocol),
                    parent("plan", "perspective-review-plan", plan),
                ],
            ),
            "review_id": "PV1",
            "project_id": "P1",
            "document_id": "D1",
            "version_id": "V1",
            "lens": {
                "kind": "perspective",
                "id": "methodological-individualism",
                "protocol_sha256": "3" * 64,
            },
            "review_scope": {
                "kind": "thesis-chain",
                "claim_ids": [],
                "selected_claim_ids": ["C1"],
            },
            "reviewed_ir_record": {
                "relative_path": "reviewed-ir-record.json",
                "sha256": "1" * 64,
            },
            "target_ir": {
                "relative_path": "target-argument-ir.json",
                "sha256": "2" * 64,
            },
            "protocol_record": {
                "relative_path": "perspective-lens-protocol.json",
                "sha256": contracts.sha256_bytes(encoded(protocol)),
            },
            "protocol": {
                "relative_path": "critic-individualist.md",
                "sha256": "3" * 64,
            },
            "plan": {
                "relative_path": "perspective-review-plan.json",
                "sha256": contracts.sha256_bytes(encoded(plan)),
            },
            "prompt": {"relative_path": "review-prompt.md", "sha256": "4" * 64},
        }
        self.assertEqual(contracts.validate_perspective_review_run(review), [])

        attempt = {
            **base(
                "perspective-result-attempt",
                "PV1-attempt-0001",
                "immutable",
                "model-derived",
                parents=[parent("review-run", "perspective-review-run", review)],
            ),
            "review_id": "PV1",
            "attempt_id": "attempt-0001",
            "collection": {
                "method": "file",
                "source_name": "results.json",
                "producer_label": "test-model",
            },
            "response": {"relative_path": "response.json", "sha256": "5" * 64},
            "validation": {"status": "valid", "errors": []},
        }
        self.assertEqual(contracts.validate_perspective_result_attempt(attempt), [])

        results = {
            "schema_version": 1,
            "artifact": "perspective-lens-results",
            "source": {
                "plan_sha256": contracts.sha256_bytes(encoded(plan)),
                "target_ir_sha256": "2" * 64,
                "protocol_sha256": "3" * 64,
            },
            "status": "complete",
            "unverified": [],
            "results": [
                {
                    "result_id": "P1",
                    "target_claim": "C1",
                    "verdict": "fail",
                    "reason": "The explanation ends at an aggregate structure.",
                    "basis_refs": ["C1", "E1"],
                    "framework_analysis": "No actor-level transition reconstructs the outcome.",
                    "consequence": "The causal bearer remains unspecified.",
                }
            ],
        }
        self.assertEqual(contracts.validate_perspective_lens_results(results), [])

        finding = {
            **base(
                "argument-finding",
                "V1-PV1-attempt-0001-F0001",
                "immutable",
                "model-derived",
                parents=[
                    {
                        "role": "target-ir",
                        "artifact": "argument-ir",
                        "sha256": "2" * 64,
                    },
                    parent("lens-result", "perspective-lens-results", results),
                ],
            ),
            "finding_id": "V1-PV1-attempt-0001-F0001",
            "target_claim": "V1:C1",
            "lens": {
                "kind": "perspective",
                "id": "methodological-individualism",
                "check_id": None,
            },
            "verdict": "fail",
            "reason": "The explanation ends at an aggregate structure.",
            "evidence_refs": ["V1:C1", "V1:E1"],
            "status": "open",
        }
        self.assertEqual(contracts.validate_argument_finding(finding), [])
        wrong_parent = copy.deepcopy(finding)
        wrong_parent["parents"][1]["artifact"] = "argument-check-results"
        self.assertTrue(
            any("perspective-lens-results" in error for error in contracts.validate_argument_finding(wrong_parent))
        )

        index = {
            **base(
                "perspective-review-index",
                "PV1-attempt-0001-perspective-review",
                "derived-replaceable",
                "deterministic",
                parents=[
                    parent("review-run", "perspective-review-run", review),
                    parent("result-attempt", "perspective-result-attempt", attempt),
                    parent("lens-result", "perspective-lens-results", results),
                    parent("finding-0001", "argument-finding", finding),
                ],
            ),
            "review_id": "PV1",
            "attempt_id": "attempt-0001",
            "version_id": "V1",
            "lens": review["lens"],
            "run_status": "complete",
            "unverified": [],
            "summary": {"pass": 0, "fail": 1, "uncertain": 0},
            "outcomes": [
                {
                    "result_id": "P1",
                    "target_claim": "V1:C1",
                    "verdict": "fail",
                    "reason": "The explanation ends at an aggregate structure.",
                    "basis_refs": ["V1:C1", "V1:E1"],
                    "framework_analysis": "No actor-level transition reconstructs the outcome.",
                    "consequence": "The causal bearer remains unspecified.",
                    "finding_id": finding["finding_id"],
                }
            ],
            "view": {"relative_path": "perspective-review.md", "sha256": "6" * 64},
            "field_provenance": {
                "outcomes": {"origin": "model-derived", "source": "lens-result"},
                "run_status": {"origin": "model-derived", "source": "lens-result"},
                "unverified": {"origin": "model-derived", "source": "lens-result"},
                "finding_id": {"origin": "deterministic", "source": "review"},
                "summary": {"origin": "deterministic", "source": "review"},
                "view": {"origin": "deterministic", "source": "review"},
            },
        }
        self.assertEqual(contracts.validate_perspective_review_index(index), [])

        duplicate_target = copy.deepcopy(results)
        duplicate_target["results"].append(copy.deepcopy(results["results"][0]))
        duplicate_target["results"][1]["result_id"] = "P2"
        self.assertTrue(
            any("at most one" in error for error in contracts.validate_perspective_lens_results(duplicate_target))
        )
        missing_target_basis = copy.deepcopy(results)
        missing_target_basis["results"][0]["basis_refs"] = ["E1"]
        self.assertTrue(
            any("include target_claim" in error for error in contracts.validate_perspective_lens_results(missing_target_basis))
        )

    def test_all_validators_tolerate_malformed_shapes(self) -> None:
        for value in (None, 1, "x", [], {}, {"artifact": "unknown"}):
            with self.subTest(value=value):
                contracts.validate_artifact(value)
                for validator in contracts.VALIDATORS.values():
                    validator(value)


if __name__ == "__main__":
    unittest.main()
