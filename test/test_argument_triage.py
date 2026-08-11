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
import argument_ir  # noqa: E402
import argument_review as review  # noqa: E402
import argument_triage as triage  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"
RULES = REPO_ROOT / "ir" / "social-science-checks.json"


class ArgumentStatusTriageTests(unittest.TestCase):
    def make_review(self, root: Path) -> tuple[workbench.WorkspacePaths, review.ReviewPaths]:
        manuscript = root / "manuscript.md"
        manuscript.write_bytes((FIXTURE / "manuscript.md").read_bytes())
        workspace = workbench.initialize_workspace(
            manuscript, root / "triage.argument-workbench"
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
        plan = json.loads(review_paths.plan.read_text(encoding="utf-8"))
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
        results = {
            "schema_version": plan["schema_version"],
            "artifact": "argument-check-results",
            "source": {
                "plan_sha256": contracts.sha256_bytes(review_paths.plan.read_bytes())
            },
            "status": "complete",
            "unverified": [],
            "results": [],
        }
        for index, task in enumerate(plan["tasks"]):
            if index == 0:
                results["results"].append(
                    {
                        "task_id": task["id"],
                        "execution_status": "routing_mismatch",
                        "verdict": None,
                        "reason": "The extracted method routes this check to the wrong Claim.",
                        "basis_refs": [task["claim_id"]],
                        "support_refs": [],
                        "support_paths": [],
                        "consequence": "",
                    }
                )
                continue
            policy = check_by_id[task["check_id"]]["evidence_policy"]
            paths = argument_ir._eligible_pass_support_paths(
                argument, task["claim_id"]
            )
            support_refs: list[str] = []
            if policy == "upstream-required":
                support_refs = [next(iter(paths))]
            elif policy == "citation-required":
                support_refs = [
                    next(ref for ref in paths if node_kinds.get(ref) == "citation")
                ]
            results["results"].append(
                {
                    "task_id": task["id"],
                    "execution_status": "evaluated",
                    "verdict": "pass",
                    "reason": "The supplied argument path satisfies this fixture check.",
                    "basis_refs": [task["claim_id"], *support_refs],
                    "support_refs": support_refs,
                    "support_paths": [
                        {"support_ref": ref, "relation_ids": paths[ref]}
                        for ref in support_refs
                    ],
                    "consequence": "",
                }
            )
        _, attempt = review.collect_review_results(
            workspace.root,
            workbench.json_bytes(results),
            review_id=review_paths.review_id,
            method="file",
            source_name="triage-results.json",
            producer_label="fixture-review-model",
        )
        self.assertEqual(attempt["validation"], {"status": "valid", "errors": []})
        return workspace, review_paths

    def test_triage_is_append_only_reproducible_and_separate_from_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace, review_paths = self.make_review(Path(temporary))
            _, attempt_id, items = triage.triage_items_for_review(workspace.root)
            self.assertEqual(attempt_id, "attempt-0001")
            self.assertEqual(len(items), 1)
            self.assertIsNone(items[0].decision)
            self.assertIn("OPEN", triage.render_status_triage(items))
            findings = list(
                review_paths.derived_attempt_dir(attempt_id).glob("findings/F*.json")
            )
            self.assertEqual(findings, [])

            with self.assertRaisesRegex(workbench.WorkbenchError, "not a valid"):
                triage.append_status_triage(
                    workspace.root,
                    task_id=items[0].task_id,
                    decision="acknowledge",
                    action="add_evidence",
                    note="Wrong follow-up for a routing mismatch.",
                )
            first = triage.append_status_triage(
                workspace.root,
                task_id=items[0].task_id,
                decision="acknowledge",
                action="correct_ir",
                note="Inspect and correct the extracted method before rerunning.",
            )
            self.assertEqual(first.name, "ST0001.json")
            index_path = first.parent.parent / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["summary"], {
                "total": 1,
                "open": 0,
                "acknowledge": 1,
                "reject": 0,
            })
            self.assertEqual(workbench.verify_workspace(workspace.root), [])

            second = triage.append_status_triage(
                workspace.root,
                task_id=items[0].task_id,
                decision="reject",
                action="rerun_review",
                note="The check applies after inspecting the Claim; rerun it.",
            )
            second_value = json.loads(second.read_text(encoding="utf-8"))
            self.assertEqual(
                second_value["supersedes"], contracts.sha256_bytes(first.read_bytes())
            )
            self.assertEqual(workbench.verify_workspace(workspace.root), [])

            second_value["note"] = "tampered human decision"
            second.write_text(
                json.dumps(second_value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(
                any(
                    "not reproducible" in error
                    for error in workbench.verify_workspace(workspace.root)
                )
            )
            triage.rebuild_status_triages(workspace.root)
            self.assertEqual(workbench.verify_workspace(workspace.root), [])

    def test_cli_triage_requires_complete_human_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace, _ = self.make_review(Path(temporary))
            with redirect_stdout(StringIO()) as stdout:
                self.assertEqual(
                    critic_runner.main(
                        ["ir", "review", "triage", str(workspace.root)]
                    ),
                    0,
                )
            self.assertIn("routing_mismatch", stdout.getvalue())
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "review",
                            "triage",
                            str(workspace.root),
                            "--task",
                            "T1",
                            "--decision",
                            "acknowledge",
                            "--action",
                            "correct_ir",
                            "--note",
                            "Correct the IR classification and rerun this review.",
                        ]
                    ),
                    0,
                )
            self.assertEqual(workbench.verify_workspace(workspace.root), [])


if __name__ == "__main__":
    unittest.main()
