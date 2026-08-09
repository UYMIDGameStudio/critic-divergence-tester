from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_adjudication as adjudication  # noqa: E402
import argument_contracts as contracts  # noqa: E402
import argument_review as review  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"
RULES = REPO_ROOT / "ir" / "social-science-checks.json"


class ArgumentAdjudicationTests(unittest.TestCase):
    def make_reviewed_project(
        self, root: Path
    ) -> tuple[workbench.WorkspacePaths, review.ReviewPaths]:
        workspace = workbench.initialize_workspace(
            FIXTURE / "manuscript.md",
            root / "adjudication demo.argument-workbench",
            title="Adjudication demo",
        )
        workbench.collect_raw_attempt(
            workspace.root,
            (FIXTURE / "raw-ir.json").read_bytes(),
            method="file",
            source_name="raw-ir.json",
            producer_label="fixture-extractor",
        )
        workbench.rebuild_workspace(workspace.root)
        review_paths, _ = review.prepare_rule_review(
            workspace.root, RULES, depth="core"
        )
        review.collect_review_results(
            workspace.root,
            (FIXTURE / "review-results.json").read_bytes(),
            review_id=review_paths.review_id,
            method="file",
            source_name="review-results.json",
            producer_label="fixture-review-model",
        )
        return workspace, review_paths

    def finding_ids(self, workspace: workbench.WorkspacePaths) -> list[str]:
        return [
            str(entry.value["finding_id"])
            for entry in adjudication.current_finding_entries(workspace.root)
        ]

    def test_accept_reject_and_revision_plan_are_separate_traceable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace, review_paths = self.make_reviewed_project(Path(temporary))
            first, second = self.finding_ids(workspace)
            finding_path = (
                review_paths.derived_attempt_dir("attempt-0001")
                / "findings"
                / "F0001.json"
            )
            original_finding = finding_path.read_bytes()
            adjudication_path, action_paths = adjudication.append_finding_decision(
                workspace.root,
                first,
                decision="accept",
                reason="The denominator criticism is persuasive.",
                actions=[
                    ("narrow_claim", "Replace the universal wording with a local claim."),
                    ("add_evidence", "Add a negative comparison case."),
                ],
            )
            self.assertEqual(adjudication_path.name, "AD0001.json")
            self.assertEqual([path.name for path in action_paths], ["RA0001.json", "RA0002.json"])
            adjudication.append_finding_decision(
                workspace.root,
                second,
                decision="reject",
                reason="The manuscript intentionally leaves the rival reading open.",
                actions=[],
            )
            paths = adjudication.human_review_paths(workspace.root)
            record = json.loads(paths.plan_record.read_text(encoding="utf-8"))
            self.assertEqual(contracts.validate_revision_plan_record(record), [])
            self.assertEqual(
                record["summary"],
                {"accept": 1, "reject": 1, "defer": 0, "open": 0},
            )
            accepted = next(item for item in record["items"] if item["decision"] == "accept")
            self.assertEqual(
                [item["action_type"] for item in accepted["actions"]],
                ["narrow_claim", "add_evidence"],
            )
            self.assertEqual(
                accepted["field_provenance"]["decision"]["origin"],
                "human-confirmed",
            )
            self.assertEqual(
                accepted["field_provenance"]["model"]["origin"],
                "model-derived",
            )
            markdown = paths.plan_markdown.read_text(encoding="utf-8")
            self.assertIn("Accepted Findings and Revision Actions", markdown)
            self.assertIn("narrow_claim", markdown)
            self.assertIn("Rejected Findings", markdown)
            self.assertNotIn("Argument Score", markdown)
            self.assertEqual(finding_path.read_bytes(), original_finding)
            finding = json.loads(original_finding)
            self.assertEqual(finding["status"], "open")
            self.assertEqual(workbench.verify_workspace(workspace), [])

            false_summary = copy.deepcopy(record)
            false_summary["summary"]["accept"] = 2
            self.assertTrue(
                any(
                    "equal items" in error
                    for error in contracts.validate_revision_plan_record(false_summary)
                )
            )
            disguised_model = copy.deepcopy(record)
            disguised_model["items"][0]["field_provenance"]["model"][
                "origin"
            ] = "deterministic"
            self.assertTrue(
                any(
                    "must remain model-derived" in error
                    for error in contracts.validate_revision_plan_record(disguised_model)
                )
            )

    def test_invalid_human_decisions_write_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace, _ = self.make_reviewed_project(Path(temporary))
            finding_id = self.finding_ids(workspace)[0]
            paths = adjudication.human_review_paths(workspace.root)
            with self.assertRaisesRegex(workbench.WorkbenchError, "revision_action"):
                adjudication.append_finding_decision(
                    workspace.root,
                    finding_id,
                    decision="accept",
                    reason="",
                    actions=[],
                )
            with self.assertRaisesRegex(workbench.WorkbenchError, "author_reason"):
                adjudication.append_finding_decision(
                    workspace.root,
                    finding_id,
                    decision="defer",
                    reason="",
                    actions=[],
                )
            self.assertFalse(paths.adjudications_dir.exists())
            self.assertFalse(paths.actions_dir.exists())
            self.assertFalse(paths.plan_dir.exists())
            self.assertEqual(workbench.verify_workspace(workspace), [])

    def test_readjudication_supersedes_without_deleting_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace, _ = self.make_reviewed_project(Path(temporary))
            finding_id = self.finding_ids(workspace)[0]
            first_path, action_paths = adjudication.append_finding_decision(
                workspace.root,
                finding_id,
                decision="accept",
                reason="Initially accepted.",
                actions=[("narrow_claim", "Narrow the claim.")],
            )
            first_bytes = first_path.read_bytes()
            action_bytes = action_paths[0].read_bytes()
            second_path, new_actions = adjudication.append_finding_decision(
                workspace.root,
                finding_id,
                decision="reject",
                reason="Rejected after checking the intended scope.",
                actions=[],
            )
            self.assertEqual(new_actions, [])
            second = json.loads(second_path.read_text(encoding="utf-8"))
            self.assertEqual(second["supersedes"], contracts.sha256_bytes(first_bytes))
            self.assertEqual(first_path.read_bytes(), first_bytes)
            self.assertEqual(action_paths[0].read_bytes(), action_bytes)
            record = json.loads(
                adjudication.human_review_paths(workspace.root).plan_record.read_text(
                    encoding="utf-8"
                )
            )
            current = next(item for item in record["items"] if item["finding_id"] == finding_id)
            self.assertEqual(current["decision"], "reject")
            self.assertEqual(current["actions"], [])
            self.assertEqual(workbench.verify_workspace(workspace), [])

    def test_scripted_adjudicator_persists_each_confirmation_and_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace, _ = self.make_reviewed_project(Path(temporary))
            answers = iter(
                [
                    "a",
                    "The universal wording is too strong.",
                    "1",
                    "Narrow 'always' to the observed cases.",
                    "n",
                    "q",
                ]
            )
            output: list[str] = []
            self.assertEqual(
                adjudication.run_adjudicator(
                    workspace.root,
                    review_id=None,
                    review_all=False,
                    view_only=False,
                    input_fn=lambda _: next(answers),
                    output_fn=output.append,
                ),
                0,
            )
            paths = adjudication.human_review_paths(workspace.root)
            self.assertTrue((paths.adjudications_dir / "AD0001.json").is_file())
            partial = json.loads(paths.plan_record.read_text(encoding="utf-8"))
            self.assertEqual(partial["summary"]["accept"], 1)
            self.assertEqual(partial["summary"]["open"], 1)
            self.assertTrue(any("Progress preserved" in line for line in output))

            resume_answers = iter(["d", "More rival-reading evidence is needed."])
            self.assertEqual(
                adjudication.run_adjudicator(
                    workspace.root,
                    review_id=None,
                    review_all=False,
                    view_only=False,
                    input_fn=lambda _: next(resume_answers),
                    output_fn=output.append,
                ),
                0,
            )
            complete = json.loads(paths.plan_record.read_text(encoding="utf-8"))
            self.assertEqual(complete["summary"]["open"], 0)
            self.assertEqual(complete["summary"]["defer"], 1)
            self.assertEqual(workbench.verify_workspace(workspace), [])

    def test_revision_plan_tamper_is_detected_and_rebuildable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace, _ = self.make_reviewed_project(Path(temporary))
            finding_id = self.finding_ids(workspace)[0]
            adjudication.append_finding_decision(
                workspace.root,
                finding_id,
                decision="accept",
                reason="Accepted.",
                actions=[("narrow_claim", "Narrow the scope.")],
            )
            paths = adjudication.human_review_paths(workspace.root)
            paths.plan_markdown.write_text("tampered", encoding="utf-8")
            self.assertTrue(
                any(
                    "revision-plan.md is not reproducible" in error
                    for error in workbench.verify_workspace(workspace)
                )
            )
            _, changed = adjudication.rebuild_revision_plan(workspace.root)
            self.assertTrue(changed)
            self.assertEqual(workbench.verify_workspace(workspace), [])

    def test_workbench_adjudication_cli_view_and_plan_do_not_edit_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace, _ = self.make_reviewed_project(Path(temporary))
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(
                    critic_runner.main(
                        ["ir", "adjudicate", str(workspace.root), "--view-only"]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        ["ir", "revision-plan", str(workspace.root), "--show"]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        ["ir", "verify-project", str(workspace.root)]
                    ),
                    0,
                )
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("Human Adjudication", stdout.getvalue())
            self.assertIn("Open Findings", stdout.getvalue())
            self.assertTrue(
                adjudication.human_review_paths(workspace.root).plan_markdown.is_file()
            )


if __name__ == "__main__":
    unittest.main()
