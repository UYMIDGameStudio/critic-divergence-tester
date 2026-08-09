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

import argument_adjudication as adjudication  # noqa: E402
import argument_contracts as contracts  # noqa: E402
import argument_gate as gate  # noqa: E402
import argument_review as review  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"
RULES = REPO_ROOT / "ir" / "social-science-checks.json"


class ArgumentGateTests(unittest.TestCase):
    def completed_project(
        self, root: Path, number: int, *, open_finding: bool = False
    ) -> workbench.WorkspacePaths:
        source = root / f"真实稿件-{number}.md"
        source_bytes = (FIXTURE / "manuscript.md").read_bytes() + (
            f"\n本文档的真实语料测试标识为 {number}。\n"
        ).encode("utf-8")
        source.write_bytes(source_bytes)
        workspace = workbench.initialize_workspace(
            source,
            root / f"稿件-{number}.argument-workbench",
            title=f"Private real manuscript {number}",
        )
        raw = json.loads((FIXTURE / "raw-ir.json").read_text(encoding="utf-8"))
        raw["source"] = {
            "name": source.name,
            "sha256": contracts.sha256_bytes(source_bytes),
        }
        workbench.collect_raw_attempt(
            workspace.root,
            workbench.json_bytes(raw),
            method="file",
            source_name="model-ir.json",
            producer_label="test-model",
        )
        workbench.rebuild_workspace(workspace.root)
        review_paths, _ = review.prepare_rule_review(workspace.root, RULES, depth="core")
        plan = json.loads(review_paths.plan.read_text(encoding="utf-8"))
        results = {
            "schema_version": 1,
            "artifact": "argument-check-results",
            "source": {"plan_sha256": contracts.sha256_bytes(review_paths.plan.read_bytes())},
            "status": "complete",
            "unverified": [],
            "results": [],
        }
        for index, task in enumerate(plan["tasks"]):
            verdict = "fail" if open_finding and index == 0 else "pass"
            results["results"].append(
                {
                    "task_id": task["id"],
                    "verdict": verdict,
                    "reason": "The evaluator found a test outcome for this workflow.",
                    "evidence_refs": [task["claim_id"]],
                    "consequence": "Revise this Claim." if verdict == "fail" else "",
                }
            )
        review.collect_review_results(
            workspace.root,
            workbench.json_bytes(results),
            review_id=review_paths.review_id,
            method="file",
            source_name="model-review.json",
            producer_label="test-review-model",
        )
        adjudication.rebuild_revision_plan(workspace.root)
        self.assertEqual(workbench.verify_workspace(workspace.root), [])
        return workspace

    @staticmethod
    def metrics(number: int = 0) -> dict[str, int]:
        return {key: number for key in gate.METRIC_KEYS}

    def test_gate_requires_three_to_five_unique_completed_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.completed_project(root, 1)
            second = self.completed_project(root, 2)
            with self.assertRaisesRegex(workbench.WorkbenchError, "3 to 5"):
                gate.initialize_gate(root / "too-small.product-gate-a", [first.root, second.root])
            third = self.completed_project(root, 3)
            with self.assertRaisesRegex(workbench.WorkbenchError, "unique"):
                gate.initialize_gate(
                    root / "duplicate.product-gate-a",
                    [first.root, second.root, third.root, third.root],
                )
            open_project = self.completed_project(root, 4, open_finding=True)
            with self.assertRaisesRegex(workbench.WorkbenchError, "adjudicated"):
                gate.initialize_gate(
                    root / "open.product-gate-a",
                    [second.root, third.root, open_project.root],
                )

    def test_private_corpus_assessments_and_human_decision_are_traceable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = [self.completed_project(root, number).root for number in range(1, 4)]
            paths = gate.initialize_gate(root / "evidence.product-gate-a", projects)
            self.assertEqual(gate.verify_gate(paths.root), [])
            corpus = json.loads(paths.corpus.read_text(encoding="utf-8"))
            self.assertEqual(len(corpus["entries"]), 3)
            corpus_text = paths.corpus.read_text(encoding="utf-8")
            self.assertNotIn("本文主张", corpus_text)
            report = json.loads(paths.report_record.read_text(encoding="utf-8"))
            self.assertEqual(report["readiness"]["workflows_complete"], 3)
            self.assertFalse(report["readiness"]["ready_for_human_decision"])
            self.assertIsNone(report["gate_decision"])
            with self.assertRaisesRegex(workbench.WorkbenchError, "cannot pass"):
                gate.append_gate_decision(paths.root, "pass", "Not assessed yet.")

            comparisons = ("clearer", "same", "uncertain")
            burdens = ("acceptable", "acceptable", "uncertain")
            for index, (comparison, burden) in enumerate(zip(comparisons, burdens), 1):
                output = gate.append_assessment(
                    paths.root,
                    f"P{index}",
                    comparison_to_direct_chat=comparison,
                    correction_burden=burden,
                    metrics=self.metrics(index),
                    regression_anchors=[f"Known important Claim {index}"],
                    actual_revision_notes=("The author narrowed one Claim." if index == 1 else ""),
                    notes=f"Human observation {index}",
                )
                self.assertEqual(output.name, f"AS{index:04d}.json")
            with self.assertRaisesRegex(workbench.WorkbenchError, "already exists"):
                gate.append_assessment(
                    paths.root,
                    "P1",
                    comparison_to_direct_chat="clearer",
                    correction_burden="acceptable",
                    metrics=self.metrics(),
                    regression_anchors=["Known important Claim 1"],
                    actual_revision_notes="",
                    notes="duplicate",
                )
            decision = gate.append_gate_decision(
                paths.root,
                "pass",
                "The human evaluator accepts the evidence after all workflows and assessments.",
            )
            self.assertEqual(decision.name, "GD0001.json")
            report = json.loads(paths.report_record.read_text(encoding="utf-8"))
            self.assertTrue(report["readiness"]["ready_for_human_decision"])
            self.assertEqual(report["gate_decision"], "pass")
            markdown = paths.report_markdown.read_text(encoding="utf-8")
            self.assertIn("not a manuscript quality score", markdown)
            self.assertIn("`pass` `[human-confirmed]`", markdown)
            self.assertEqual(gate.verify_gate(paths.root), [])

    def test_gate_detects_changed_workspaces_and_rebuilds_only_derived_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = [self.completed_project(root, number) for number in range(1, 4)]
            paths = gate.initialize_gate(
                root / "tamper.product-gate-a", [project.root for project in projects]
            )
            paths.report_markdown.write_text("tampered\n", encoding="utf-8")
            self.assertTrue(any("not reproducible" in error for error in gate.verify_gate(paths.root)))
            _, changed = gate.rebuild_gate_report(paths.root)
            self.assertTrue(changed)
            self.assertEqual(gate.verify_gate(paths.root), [])

            version = json.loads(projects[0].version.read_text(encoding="utf-8"))
            source = projects[0].version_dir / version["source"]["relative_path"]
            source.write_bytes(source.read_bytes() + b"changed")
            errors = gate.verify_gate(paths.root)
            self.assertTrue(any("source hash" in error or "invalid" in error for error in errors))
            with self.assertRaisesRegex(workbench.WorkbenchError, "cannot rebuild"):
                gate.rebuild_gate_report(paths.root)

    def test_gate_cli_creates_reports_without_copying_manuscripts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = [self.completed_project(root, number).root for number in range(1, 4)]
            output = root / "CLI Gate.product-gate-a"
            stream = StringIO()
            with redirect_stdout(stream):
                self.assertEqual(
                    critic_runner.main(
                        ["ir", "gate-a", "init", str(output), *map(str, projects)]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(["ir", "gate-a", "report", str(output), "--show"]),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "gate-a",
                            "assess",
                            str(output),
                            "P1",
                            "--comparison",
                            "clearer",
                            "--burden",
                            "acceptable",
                            "--correction-minutes",
                            "10",
                            "--missed-claims",
                            "1",
                            "--wrong-claim-types",
                            "0",
                            "--wrong-relations",
                            "0",
                            "--rhetoric-as-claims",
                            "0",
                            "--reversed-attributions",
                            "0",
                            "--anchor",
                            "Known Claim regression anchor",
                            "--actual-revision-notes",
                            "The author narrowed one Claim.",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(["ir", "gate-a", "verify", str(output)]),
                    0,
                )
            rendered = stream.getvalue()
            self.assertIn("manuscript bytes were not copied", rendered)
            self.assertIn('"valid": true', rendered)


if __name__ == "__main__":
    unittest.main()
