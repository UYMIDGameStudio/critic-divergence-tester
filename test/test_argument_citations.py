from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_citations as citations  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"
STAMP = "2026-08-23T00:00:00+00:00"


class ArgumentCitationTests(unittest.TestCase):
    def make_project(self, root: Path, *, cite_evidence: bool = False):
        manuscript = root / "manuscript.md"
        manuscript.write_bytes((FIXTURE / "manuscript.md").read_bytes())
        paths = workbench.initialize_workspace(manuscript, project_dir=root / "demo.argument-workbench")
        raw = json.loads((FIXTURE / "raw-ir.json").read_text(encoding="utf-8"))
        if cite_evidence:
            raw["relations"][4]["to"] = "E1"
        workbench.collect_raw_attempt(
            paths,
            workbench.json_bytes(raw),
            method="file",
            source_name="raw-ir.json",
            producer_label="extractor",
        )
        workbench.rebuild_workspace(paths)
        return paths

    def result(self, paths: citations.CitationAuditPaths):
        return {
            "schema_version": 1,
            "artifact": "citation-audit-results",
            "source": citations._expected_result_source(paths),
            "status": "complete",
            "sources": [
                {
                    "source_id": "S1",
                    "kind": "primary",
                    "title": "Girard exact source",
                    "locator": "https://example.test/girard",
                    "accessed_at": STAMP,
                    "dimensions": [
                        "bibliographic_existence",
                        "exact_source_located",
                        "content_support",
                        "context_preserved",
                    ],
                    "note": "The exact edition and surrounding passage were inspected.",
                }
            ],
            "outcomes": [
                {
                    "citation_id": "Z1",
                    "bibliographic_existence": "verified",
                    "exact_source_located": "verified",
                    "content_support": "supports",
                    "context_preserved": "yes",
                    "reason": "The exact passage supports the manuscript wording in context.",
                    "source_refs": ["S1"],
                    "uncertainty": "",
                }
            ],
            "unverified": [],
        }

    def test_model_proposal_remains_unverified_until_human_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            audit, created = citations.prepare_citation_audit(project.root)
            self.assertTrue(created)
            prompt = audit.prompt.read_text(encoding="utf-8")
            self.assertIn("four epistemic questions", prompt)
            self.assertIn("never establishes claim_false", prompt)
            _, attempt = citations.collect_citation_results(
                project.root,
                workbench.json_bytes(self.result(audit)),
                audit_id=audit.audit_id,
                version_id="V1",
                method="file",
                source_name="citation-results.json",
                producer_label="citation-model",
            )
            self.assertEqual(attempt["validation"], {"status": "valid", "errors": []})
            index = json.loads(audit.index.read_text(encoding="utf-8"))
            self.assertEqual(index["citations"][0]["current_status"], "model_proposed")
            self.assertEqual(index["citations"][0]["verification_state"], "unverified")
            self.assertTrue(
                all(
                    item["status"] == "depends_on_unverified_evidence"
                    for item in index["claim_dependencies"]
                )
            )

            decision_path = citations.append_citation_decision(
                project.root,
                audit_id=audit.audit_id,
                version_id="V1",
                citation_id="Z1",
                decision="confirm",
                reason="The human inspected the exact source and confirms all four dimensions.",
            )
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            self.assertEqual(decision["provenance"]["origin"], "human-confirmed")
            index = json.loads(audit.index.read_text(encoding="utf-8"))
            self.assertEqual(index["summary"]["verified"], 1)
            self.assertTrue(
                all(item["status"] == "citation_verified" for item in index["claim_dependencies"])
            )
            report = audit.report.read_text(encoding="utf-8")
            self.assertIn("Bibliographic existence: ✓", report)
            self.assertIn("does **not** establish `claim_false`", report)
            self.assertEqual(citations.verify_citation_audits(project.root), [])
            self.assertEqual(workbench.verify_project_versions(project.root), [])

            audit.report.write_text("tampered\n", encoding="utf-8")
            self.assertTrue(
                any("not reproducible" in error for error in citations.verify_citation_audits(project.root))
            )
            _, changed = citations.rebuild_citation_audit(audit)
            self.assertTrue(changed)
            self.assertEqual(citations.verify_citation_audits(project.root), [])

    def test_citation_to_evidence_to_claim_dependency_and_human_correction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary), cite_evidence=True)
            audit, _ = citations.prepare_citation_audit(project.root)
            citations.collect_citation_results(
                project.root,
                workbench.json_bytes(self.result(audit)),
                audit_id=None,
                version_id=None,
                method="file",
                source_name="citation-results.json",
                producer_label="citation-model",
            )
            citations.append_citation_decision(
                project.root,
                audit_id=None,
                version_id=None,
                citation_id="Z1",
                decision="confirm",
                reason="The initial proposal was checked.",
            )
            corrected = {
                "bibliographic_existence": "verified",
                "exact_source_located": "verified",
                "content_support": "does_not_support",
                "context_preserved": "yes",
                "uncertainty": "The source exists, but it does not support this wording.",
            }
            second = citations.append_citation_decision(
                project.root,
                audit_id=None,
                version_id=None,
                citation_id="Z1",
                decision="correct",
                reason="Human reading rejects the model's content-support judgment.",
                final_outcome=corrected,
            )
            second_value = json.loads(second.read_text(encoding="utf-8"))
            self.assertIsNotNone(second_value["supersedes"])
            index = json.loads(audit.index.read_text(encoding="utf-8"))
            self.assertEqual(index["evidence_dependencies"][0]["node_id"], "E1")
            self.assertEqual(
                index["evidence_dependencies"][0]["status"],
                "depends_on_unverified_evidence",
            )
            self.assertGreaterEqual(len(index["claim_dependencies"]), 1)
            self.assertNotIn("claim_false", json.dumps(index))
            self.assertEqual(citations.verify_citation_audits(project.root), [])

    def test_invalid_attempt_is_retained_without_derived_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            audit, _ = citations.prepare_citation_audit(project.root)
            invalid = self.result(audit)
            invalid["outcomes"][0]["exact_source_located"] = "not_verified"
            attempt_dir, attempt = citations.collect_citation_results(
                project.root,
                workbench.json_bytes(invalid),
                audit_id=None,
                version_id=None,
                method="file",
                source_name="bad.json",
                producer_label=None,
            )
            self.assertEqual(attempt["validation"]["status"], "unusable")
            self.assertTrue((attempt_dir / "response.json").is_file())
            self.assertFalse(audit.index.exists())
            with self.assertRaises(workbench.WorkbenchError):
                citations.append_citation_decision(
                    project.root,
                    audit_id=None,
                    version_id=None,
                    citation_id="Z1",
                    decision="confirm",
                    reason="Cannot confirm an invalid attempt.",
                )
            self.assertEqual(citations.verify_citation_audits(project.root), [])

    def test_bundled_offline_demo_preserves_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            audit, _ = citations.prepare_citation_audit(project.root)
            _, attempt = citations.collect_citation_results(
                project.root,
                (FIXTURE / "citation-audit-results.json").read_bytes(),
                audit_id=None,
                version_id=None,
                method="file",
                source_name="citation-audit-results.json",
                producer_label="offline-regression",
            )
            self.assertEqual(attempt["validation"], {"status": "valid", "errors": []})
            citations.append_citation_decision(
                project.root,
                audit_id=None,
                version_id=None,
                citation_id="Z1",
                decision="confirm",
                reason="Human confirms that all four dimensions remain unverified.",
            )
            index = json.loads(audit.index.read_text(encoding="utf-8"))
            self.assertEqual(index["summary"]["verified"], 0)
            self.assertEqual(index["summary"]["unverified"], 1)
            self.assertGreater(index["summary"]["claims_depending_on_unverified_evidence"], 0)
            self.assertEqual(citations.verify_citation_audits(project.root), [])

    def test_citation_cli_vertical_slice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    critic_runner.main(["ir", "citations", "prepare", str(project.root)]),
                    0,
                )
            audit = citations.selected_citation_audit(project.root, None)
            result_path = Path(temporary) / "citation-results.json"
            result_path.write_bytes(workbench.json_bytes(self.result(audit)))
            with redirect_stdout(output):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "citations",
                            "collect",
                            str(project.root),
                            "--file",
                            str(result_path),
                            "--producer-label",
                            "test-model",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "citations",
                            "decide",
                            str(project.root),
                            "--citation",
                            "Z1",
                            "--decision",
                            "confirm",
                            "--reason",
                            "Human checked the source.",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(["ir", "citations", "show", str(project.root)]),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(["ir", "citations", "rebuild", str(project.root)]),
                    0,
                )
            rendered = output.getvalue()
            self.assertIn("Citation Audit", rendered)
            self.assertIn("Human Citation decision", rendered)
            self.assertIn("Claim dependencies", rendered)


if __name__ == "__main__":
    unittest.main()
