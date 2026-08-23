from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_contracts as contracts  # noqa: E402
import argument_lineage as lineage  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


class ArgumentLineageTests(unittest.TestCase):
    def make_two_versions(self, root: Path):
        from test.test_argument_versioning import ArgumentVersioningTests

        return ArgumentVersioningTests().make_two_versions(root)

    def proposal(self, paths: lineage.LineageAnalysisPaths) -> dict[str, object]:
        _, from_bytes = workbench._read_json(paths.from_ir)
        _, to_bytes = workbench._read_json(paths.to_ir)
        _, diff_bytes = workbench._read_json(paths.structural_diff)
        pairs = [
            ("C1", "C2", "modified", ["scope_narrowed"]),
            ("C2", "C1", "modified", ["argument_role_changed"]),
            ("C3", "C3", "unchanged", []),
        ]
        return {
            "schema_version": 1,
            "artifact": "claim-lineage-proposals",
            "source": {
                "structural_diff_sha256": contracts.sha256_bytes(diff_bytes),
                "from_ir_sha256": contracts.sha256_bytes(from_bytes),
                "to_ir_sha256": contracts.sha256_bytes(to_bytes),
            },
            "status": "complete",
            "unverified": [],
            "proposals": [
                {
                    "proposal_id": f"LP{index}",
                    "from_claims": [f"V1:{old}"],
                    "to_claims": [f"V2:{new}"],
                    "relation": relation,
                    "semantic_changes": changes,
                    "reason": "Compared the exact Claim texts and roles.",
                    "basis_refs": [f"V1:{old}", f"V2:{new}"],
                    "uncertainty": "",
                }
                for index, (old, new, relation, changes) in enumerate(pairs, 1)
            ],
        }

    def test_prepare_collect_derive_and_verify_model_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, v2 = self.make_two_versions(Path(temporary))
            paths, created = lineage.prepare_lineage_analysis(v2.root)
            self.assertTrue(created)
            self.assertIn("human confirms", paths.prompt.read_text(encoding="utf-8"))
            same, created_again = lineage.prepare_lineage_analysis(v2.root)
            self.assertFalse(created_again)
            self.assertEqual(same.root, paths.root)

            payload = self.proposal(paths)
            attempt_dir, attempt = lineage.collect_lineage_proposals(
                v2.root,
                workbench.json_bytes(payload),
                method="file",
                source_name="lineage.json",
                producer_label="test-model",
            )
            self.assertEqual(attempt["validation"], {"status": "valid", "errors": []})
            derived = paths.derived_dir("attempt-0001")
            index = json.loads((derived / "claim-lineage-index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["summary"]["total"], 3)
            proposed = json.loads((derived / "lineages" / "L0001.json").read_text(encoding="utf-8"))
            self.assertEqual(proposed["schema_version"], 2)
            self.assertEqual(proposed["status"], "proposed")
            self.assertEqual(proposed["provenance"]["origin"], "model-derived")
            self.assertIn("proposal-result", {parent["role"] for parent in proposed["parents"]})
            self.assertEqual(lineage.verify_lineage_analyses(v2.root), [])
            self.assertEqual(workbench.verify_project_versions(v2.root), [])

            before = {path.relative_to(derived): path.read_bytes() for path in derived.rglob("*") if path.is_file()}
            _, changed = lineage.rebuild_lineage_attempt(paths, "attempt-0001")
            self.assertFalse(changed)
            after = {path.relative_to(derived): path.read_bytes() for path in derived.rglob("*") if path.is_file()}
            self.assertEqual(before, after)
            self.assertTrue(attempt_dir.is_dir())

    def test_incomplete_or_wrong_version_proposals_are_preserved_but_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, v2 = self.make_two_versions(Path(temporary))
            paths, _ = lineage.prepare_lineage_analysis(v2.root)
            payload = self.proposal(paths)
            payload["proposals"] = payload["proposals"][:1]
            payload["proposals"][0]["to_claims"] = ["V1:C2"]
            payload["proposals"][0]["basis_refs"] = ["V1:C1", "V1:C2"]
            attempt_dir, attempt = lineage.collect_lineage_proposals(
                v2.root,
                workbench.json_bytes(payload),
                method="file",
                source_name="bad.json",
                producer_label=None,
            )
            self.assertEqual(attempt["validation"]["status"], "unusable")
            self.assertTrue((attempt_dir / "response.json").is_file())
            self.assertFalse(paths.derived_dir("attempt-0001").exists())
            self.assertEqual(lineage.verify_lineage_analyses(v2.root), [])

    def test_cli_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, v2 = self.make_two_versions(Path(temporary))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(critic_runner.main(["ir", "lineage", "prepare", str(v2.root)]), 0)
            paths = lineage.selected_lineage_analysis(v2.root)
            proposal_file = Path(temporary) / "proposal.json"
            proposal_file.write_bytes(workbench.json_bytes(self.proposal(paths)))
            with redirect_stdout(output):
                self.assertEqual(critic_runner.main(["ir", "lineage", "collect", str(v2.root), "--file", str(proposal_file), "--producer-label", "cli-model"]), 0)
                self.assertEqual(critic_runner.main(["ir", "lineage", "show", str(v2.root)]), 0)
            self.assertIn("Semantic Claim Lineage Proposals", output.getvalue())
            view = paths.derived_dir("attempt-0001") / "claim-lineage.md"
            view.write_text("tampered\n", encoding="utf-8")
            self.assertTrue(any("not reproducible" in error for error in lineage.verify_lineage_analyses(v2.root)))
            _, changed = lineage.rebuild_lineage_attempt(paths, "attempt-0001")
            self.assertTrue(changed)
            self.assertEqual(lineage.verify_lineage_analyses(v2.root), [])

    def test_human_decisions_are_append_only_and_corrections_do_not_edit_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, v2 = self.make_two_versions(Path(temporary))
            paths, _ = lineage.prepare_lineage_analysis(v2.root)
            lineage.collect_lineage_proposals(
                v2.root, workbench.json_bytes(self.proposal(paths)),
                method="file", source_name="lineage.json", producer_label="model",
            )
            proposal_path = paths.derived_dir("attempt-0001") / "lineages" / "L0001.json"
            original = proposal_path.read_bytes()
            first = lineage.append_lineage_decision(
                v2.root, proposal_ids=["LP1"], decision="confirm",
                human_note="The correspondence is right.",
            )[0]
            corrected = lineage.append_lineage_decision(
                v2.root, proposal_ids=["LP1"], decision="correct",
                human_note="The descendant is C1, not C2.",
                correction={
                    "from_claims": ["V1:C1"], "to_claims": ["V2:C1"],
                    "relation": "modified", "semantic_changes": ["concept_reframed"],
                    "reason": "Human-corrected semantic correspondence.",
                    "basis_refs": ["V1:C1", "V2:C1"], "uncertainty": "",
                },
            )[0]
            self.assertEqual(proposal_path.read_bytes(), original)
            first_value = json.loads(first.read_text(encoding="utf-8"))
            corrected_value = json.loads(corrected.read_text(encoding="utf-8"))
            self.assertEqual(corrected_value["supersedes_sha256"], contracts.sha256_bytes(first.read_bytes()))
            self.assertEqual(corrected_value["provenance"]["origin"], "human-confirmed")
            self.assertEqual(corrected_value["review_action"], "correct")
            self.assertEqual(first_value["status"], "human_confirmed")
            history = lineage.render_lineage_history(v2.root)
            self.assertIn("human_confirmed (correct)", history)
            self.assertIn("Human correction", history)
            self.assertEqual(lineage.verify_lineage_analyses(v2.root), [])

    def test_cli_batch_decision_has_an_expected_count_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, v2 = self.make_two_versions(Path(temporary))
            paths, _ = lineage.prepare_lineage_analysis(v2.root)
            lineage.collect_lineage_proposals(
                v2.root, workbench.json_bytes(self.proposal(paths)),
                method="file", source_name="lineage.json", producer_label="model",
            )
            self.assertNotEqual(
                critic_runner.main([
                    "ir", "lineage", "adjudicate", str(v2.root), "--all",
                    "--expected-count", "2", "--decision", "confirm",
                    "--reason", "Reviewed together.",
                ]),
                0,
            )
            self.assertEqual(lineage.list_lineage_decisions(paths), [])
            self.assertEqual(
                critic_runner.main([
                    "ir", "lineage", "adjudicate", str(v2.root), "--all",
                    "--expected-count", "3", "--decision", "confirm",
                    "--reason", "Reviewed all three proposals.",
                ]),
                0,
            )
            self.assertEqual(len(lineage.list_lineage_decisions(paths)), 3)
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    critic_runner.main(["ir", "lineage", "history", str(v2.root)]), 0
                )
            self.assertIn("human_confirmed (confirm)", output.getvalue())
            self.assertEqual(lineage.verify_lineage_analyses(v2.root), [])


if __name__ == "__main__":
    unittest.main()
