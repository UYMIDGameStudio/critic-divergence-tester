from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_gate_b as gate_b  # noqa: E402
import argument_resolution as resolution  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


class ProductGateBTests(unittest.TestCase):
    def completed_project(self, root: Path, *, split: bool, verdicts):
        from test.test_argument_resolution import ArgumentResolutionTests

        helper = ArgumentResolutionTests()
        _, v2, finding_id = helper.make_chain(root, split=split)
        paths, _ = resolution.prepare_resolution(v2.root, finding_id, from_version="V1", to_version="V2")
        resolution.collect_resolution_results(v2.root, workbench.json_bytes(helper.result(paths, verdicts)), resolution_id=None, method="file", source_name="retest.json", producer_label="model")
        resolution.append_resolution_decision(v2.root, resolution_id=None, decision="confirm", reason="Human checked the original-Lens retest.")
        return v2.root

    def test_gate_b_binds_real_workflow_state_and_remains_a_human_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            p1 = self.completed_project(root / "one", split=True, verdicts=["pass", "fail"])
            p2 = self.completed_project(root / "two", split=False, verdicts="fail")
            gate = gate_b.initialize_gate_b(root / "gate-b", [p1, p2])
            corpus = json.loads(gate.corpus.read_text(encoding="utf-8"))
            self.assertEqual(len(corpus["projects"]), 2)
            self.assertIn("split", corpus["projects"][0]["observed_relations"])
            self.assertTrue(gate_b.gate_b_readiness(gate.root))
            gate_b.append_gate_b_assessment(gate.root, "P1", lineage_correction_minutes=8, lineage_reasonable="yes", split_merge_worked="yes", finding_inheritance_correct="yes", resolved_stopped_reappearing="yes", unresolved_persisted="not_observed", revision_rationale_clarity="clear", notes="Split descendants remained understandable.")
            gate_b.append_gate_b_assessment(gate.root, "P2", lineage_correction_minutes=3, lineage_reasonable="yes", split_merge_worked="not_observed", finding_inheritance_correct="yes", resolved_stopped_reappearing="not_observed", unresolved_persisted="yes", revision_rationale_clarity="clear", notes="Unresolved Finding remained visible.")
            self.assertEqual(gate_b.gate_b_readiness(gate.root), [])
            decision = gate_b.append_gate_b_decision(gate.root, "pass", "Both authors completed the multi-version workflow and understood the history.")
            self.assertTrue(decision.is_file())
            report = gate.report_markdown.read_text(encoding="utf-8")
            self.assertIn("Gate decision: `pass`", report)
            self.assertIn("No score is computed", report)
            self.assertEqual(gate_b.verify_gate_b(gate.root), [])

    def test_gate_b_refuses_automatic_pass_and_detects_changed_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            p1 = self.completed_project(root / "one", split=True, verdicts=["pass", "fail"])
            p2 = self.completed_project(root / "two", split=False, verdicts="fail")
            gate = gate_b.initialize_gate_b(root / "gate-b", [p1, p2])
            with self.assertRaises(workbench.WorkbenchError):
                gate_b.append_gate_b_decision(gate.root, "pass", "Too early.")
            self.assertFalse(any(gate.decisions.iterdir()))
            resolution.append_resolution_decision(p1, resolution_id=None, decision="correct", final_status="uncertain", reason="Reconsidered after Gate snapshot.")
            self.assertTrue(any("bound project state changed" in error for error in gate_b.verify_gate_b(gate.root)))

    def test_gate_b_cli_help_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            p1 = self.completed_project(root / "one", split=True, verdicts=["pass", "fail"])
            p2 = self.completed_project(root / "two", split=False, verdicts="fail")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(critic_runner.main(["ir", "gate-b", "init", str(root / "gate"), str(p1), str(p2)]), 0)
                self.assertEqual(critic_runner.main(["ir", "gate-b", "report", str(root / "gate"), "--show"]), 0)
            self.assertIn("Product Gate B", output.getvalue())

    def test_gate_b_initialization_never_publishes_a_partial_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            p1 = self.completed_project(root / "one", split=True, verdicts=["pass", "fail"])
            p2 = self.completed_project(root / "two", split=False, verdicts="fail")
            output = root / "gate-b"
            original_mkdir = Path.mkdir

            for failing_directory in ("assessments", "decisions", "report"):
                with self.subTest(failing_directory=failing_directory):
                    def fail_one_step(path, *args, **kwargs):
                        if path.name == failing_directory and path.parent.name.startswith(".gate-b."):
                            raise RuntimeError("simulated initialization crash")
                        return original_mkdir(path, *args, **kwargs)

                    with patch.object(Path, "mkdir", fail_one_step):
                        with self.assertRaisesRegex(RuntimeError, "simulated initialization crash"):
                            gate_b.initialize_gate_b(output, [p1, p2])
                    self.assertFalse(output.exists())
                    self.assertEqual(list(root.glob(".gate-b.*")), [])

            with patch.object(gate_b, 'rebuild_gate_b_report', side_effect=RuntimeError('report crash')):
                with self.assertRaisesRegex(RuntimeError, 'report crash'):
                    gate_b.initialize_gate_b(output, [p1, p2])
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob('.gate-b.*')), [])

            gate = gate_b.initialize_gate_b(output, [p1, p2])
            self.assertTrue(gate.corpus.is_file())
            self.assertTrue(gate.assessments.is_dir())
            self.assertTrue(gate.decisions.is_dir())
            self.assertTrue(gate.report_dir.is_dir())
            self.assertEqual(gate_b.verify_gate_b(output), [])


if __name__ == "__main__":
    unittest.main()
