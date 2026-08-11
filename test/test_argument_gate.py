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
import argument_baseline as baseline  # noqa: E402
import argument_contracts as contracts  # noqa: E402
import argument_gate as gate  # noqa: E402
import argument_ir  # noqa: E402
import argument_review as review  # noqa: E402
import argument_triage as triage  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"
RULES = REPO_ROOT / "ir" / "social-science-checks.json"


class ArgumentGateTests(unittest.TestCase):
    def completed_project(
        self,
        root: Path,
        number: int,
        *,
        open_finding: bool = False,
        with_baseline: bool = True,
        routing_mismatch: bool = False,
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
            "schema_version": plan["schema_version"],
            "artifact": "argument-check-results",
            "source": {"plan_sha256": contracts.sha256_bytes(review_paths.plan.read_bytes())},
            "status": "complete",
            "unverified": [],
            "results": [],
        }
        check_by_id = {check["id"]: check for check in plan["checks"]}
        argument = plan["argument_ir"]
        node_kinds = {
            item["id"]: kind
            for kind, field in (
                ("claim", "claims"),
                ("evidence", "evidence"),
                ("assumption", "assumptions"),
                ("citation", "citations"),
            )
            for item in argument[field]
        }
        for index, task in enumerate(plan["tasks"]):
            if routing_mismatch and index == 0:
                results["results"].append(
                    {
                        "task_id": task["id"],
                        "execution_status": "routing_mismatch",
                        "verdict": None,
                        "reason": "The extracted method routes this check incorrectly.",
                        "basis_refs": [task["claim_id"]],
                        "support_refs": [],
                        "support_paths": [],
                        "consequence": "",
                    }
                )
                continue
            verdict = "fail" if open_finding and index == 0 else "pass"
            policy = check_by_id[task["check_id"]]["evidence_policy"]
            eligible_paths = argument_ir._eligible_pass_support_paths(
                argument, task["claim_id"]
            )
            support_refs = []
            if verdict == "pass" and policy == "upstream-required":
                support_refs = [
                    next(iter(eligible_paths))
                ]
            elif verdict == "pass" and policy == "citation-required":
                support_refs = [
                    next(
                        ref
                        for ref in eligible_paths
                        if node_kinds.get(ref) == "citation"
                    )
                ]
            results["results"].append(
                {
                    "task_id": task["id"],
                    "execution_status": "evaluated",
                    "verdict": verdict,
                    "reason": "The evaluator found a test outcome for this workflow.",
                    "basis_refs": [task["claim_id"], *support_refs],
                    "support_refs": support_refs,
                    "support_paths": [
                        {
                            "support_ref": ref,
                            "relation_ids": eligible_paths[ref],
                        }
                        for ref in support_refs
                    ],
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
        prompt = root / f"direct-prompt-{number}.md"
        response = root / f"direct-response-{number}.md"
        prompt.write_text("Review this complete manuscript directly.\n", encoding="utf-8")
        response.write_text(
            f"Direct review response for manuscript {number}.\n", encoding="utf-8"
        )
        if with_baseline:
            baseline.collect_direct_review_baseline(
                workspace.root,
                prompt_file=prompt,
                response_file=response,
                model_label="test-model-v1",
                started_at="2026-08-10T10:00:00+08:00",
                completed_at="2026-08-10T10:02:00+08:00",
                producer_label="test-direct-model",
            )
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

    def test_readiness_reports_incomplete_projects_without_creating_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.completed_project(root, 1)
            second = self.completed_project(root, 2)
            third = self.completed_project(root, 3, open_finding=True)
            projects = [first.root, second.root, third.root]
            readiness = gate.gate_readiness(projects)
            self.assertEqual(readiness["summary"]["projects"], 3)
            self.assertEqual(readiness["summary"]["ready_for_capture"], 2)
            self.assertEqual(readiness["summary"]["open_findings"], 1)
            self.assertFalse(readiness["summary"]["duplicate_sources"])
            self.assertFalse(readiness["summary"]["can_capture_corpus"])
            blocked = readiness["projects"][2]
            self.assertEqual(blocked["model_findings"], {"fail": 1, "uncertain": 0})
            self.assertEqual(
                blocked["human_decisions"],
                {"accept": 0, "reject": 0, "defer": 0, "open": 1},
            )
            self.assertIn("--summary-only", blocked["next_command"])
            rendered = gate.render_gate_readiness(readiness)
            self.assertIn("2/3 projects ready; 1 open Findings", rendered)
            self.assertIn("Can capture immutable corpus: no", rendered)
            self.assertIn("does not create an assessment", rendered)

            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    critic_runner.main(
                        ["ir", "gate-a", "readiness", *map(str, projects)]
                    ),
                    0,
                )
            self.assertIn("Product Gate A readiness (read-only)", stdout.getvalue())
            self.assertFalse(any(root.glob("*.product-gate-a")))
            for project in projects:
                self.assertEqual(workbench.verify_workspace(project), [])

    def test_readiness_requires_a_bound_direct_review_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = [
                self.completed_project(root, 1).root,
                self.completed_project(root, 2).root,
                self.completed_project(root, 3, with_baseline=False).root,
            ]
            readiness = gate.gate_readiness(projects)
            self.assertEqual(readiness["summary"]["ready_for_capture"], 2)
            missing = readiness["projects"][2]
            self.assertFalse(missing["direct_review_baseline"])
            self.assertIn("ir gate-a baseline", missing["next_command"])
            with self.assertRaisesRegex(workbench.WorkbenchError, "no direct-review"):
                gate.initialize_gate(root / "missing.product-gate-a", projects)

    def test_readiness_requires_human_triage_for_non_evaluated_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = [
                self.completed_project(root, 1).root,
                self.completed_project(root, 2).root,
                self.completed_project(root, 3, routing_mismatch=True).root,
            ]
            readiness = gate.gate_readiness(projects)
            blocked = readiness["projects"][2]
            self.assertEqual(blocked["status_triage"]["open"], 1)
            self.assertIn("ir review triage", blocked["next_command"])
            with self.assertRaisesRegex(workbench.WorkbenchError, "human triage"):
                gate.initialize_gate(root / "untriaged.product-gate-a", projects)

            _, _, items = triage.triage_items_for_review(projects[2])
            triage.append_status_triage(
                projects[2],
                task_id=items[0].task_id,
                decision="acknowledge",
                action="correct_ir",
                note="Record the routing issue for correction and rerun it before use.",
            )
            readiness = gate.gate_readiness(projects)
            self.assertEqual(readiness["projects"][2]["status_triage"]["open"], 0)
            self.assertEqual(readiness["summary"]["ready_for_capture"], 3)
            paths = gate.initialize_gate(root / "triaged.product-gate-a", projects)
            corpus = json.loads(paths.corpus.read_text(encoding="utf-8"))
            self.assertEqual(len(corpus["entries"][2]["bindings"]["status_triage"]), 1)
            self.assertEqual(gate.verify_gate(paths.root), [])

    def test_gate_rejects_a_baseline_captured_after_workbench_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = [
                self.completed_project(root, number).root for number in range(1, 4)
            ]
            baseline.collect_direct_review_baseline(
                projects[2],
                prompt_file=root / "direct-prompt-3.md",
                response_file=root / "direct-response-3.md",
                model_label="test-model-v1",
                started_at="2030-08-10T10:00:00+08:00",
                completed_at="2030-08-10T10:02:00+08:00",
                producer_label="test-direct-model",
            )
            self.assertEqual(workbench.verify_workspace(projects[2]), [])
            with self.assertRaisesRegex(
                workbench.WorkbenchError,
                "completed after a Workbench Rule Review result",
            ):
                gate.initialize_gate(root / "contaminated.product-gate-a", projects)

    def test_private_corpus_assessments_and_human_decision_are_traceable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = [self.completed_project(root, number).root for number in range(1, 4)]
            paths = gate.initialize_gate(root / "evidence.product-gate-a", projects)
            self.assertEqual(gate.verify_gate(paths.root), [])
            corpus = json.loads(paths.corpus.read_text(encoding="utf-8"))
            self.assertEqual(corpus["schema_version"], 3)
            self.assertEqual(len(corpus["entries"]), 3)
            self.assertTrue(
                all(
                    "direct_review_baseline" in entry["bindings"]
                    for entry in corpus["entries"]
                )
            )
            self.assertTrue(
                all("status_triage" in entry["bindings"] for entry in corpus["entries"])
            )
            self.assertEqual(
                sum(
                    1
                    for parent in corpus["parents"]
                    if parent["artifact"] == "direct-review-baseline"
                ),
                3,
            )
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
                assessment = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(assessment["schema_version"], 3)
                self.assertIn(
                    "direct-review-baseline",
                    {parent["role"] for parent in assessment["parents"]},
                )
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
