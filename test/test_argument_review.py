from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_contracts as contracts  # noqa: E402
import argument_review as review  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"
RULES = REPO_ROOT / "ir" / "social-science-checks.json"


class ArgumentReviewTests(unittest.TestCase):
    def make_project(self, root: Path) -> workbench.WorkspacePaths:
        paths = workbench.initialize_workspace(
            FIXTURE / "manuscript.md",
            root / "review demo.argument-workbench",
            title="Argument review demo",
        )
        workbench.collect_raw_attempt(
            paths.root,
            (FIXTURE / "raw-ir.json").read_bytes(),
            method="file",
            source_name="raw-ir.json",
            producer_label="fixture-extractor",
        )
        workbench.rebuild_workspace(paths.root)
        return paths

    def prepare(self, paths: workbench.WorkspacePaths) -> review.ReviewPaths:
        prepared, created = review.prepare_rule_review(
            paths.root, RULES, depth="core"
        )
        self.assertTrue(created)
        return prepared

    def results(
        self,
        paths: review.ReviewPaths,
        *,
        first: str = "fail",
        second: str = "uncertain",
    ) -> dict[str, object]:
        plan = json.loads(paths.plan.read_text(encoding="utf-8"))
        verdicts = [first, second]
        items: list[dict[str, object]] = []
        for index, task in enumerate(plan["tasks"]):
            verdict = verdicts[index] if index < len(verdicts) else "pass"
            items.append(
                {
                    "task_id": task["id"],
                    "verdict": verdict,
                    "reason": f"Reason for {task['check_id']}",
                    "evidence_refs": [task["claim_id"]]
                    if verdict != "uncertain"
                    else [],
                    "consequence": "Revise or inspect this Claim."
                    if verdict in {"fail", "uncertain"}
                    else "",
                }
            )
        return {
            "schema_version": 1,
            "artifact": "argument-check-results",
            "source": {"plan_sha256": contracts.sha256_bytes(paths.plan.read_bytes())},
            "status": "complete",
            "unverified": [],
            "results": items,
        }

    @staticmethod
    def encoded(value: object) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )

    def test_prepare_collect_and_show_are_claim_centered_and_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            paths = self.prepare(workspace)
            self.assertEqual(paths.review_id, "RV1")
            repeated, created = review.prepare_rule_review(
                workspace.root, RULES, depth="core"
            )
            self.assertFalse(created)
            self.assertEqual(repeated.root, paths.root)
            self.assertTrue(paths.reviewed_record.is_file())
            self.assertTrue(paths.target_ir.is_file())

            invalid_path, invalid = review.collect_review_results(
                workspace.root,
                b'{"artifact":"argument-check-results","artifact":"duplicate"}\n',
                review_id=paths.review_id,
                method="file",
                source_name="invalid.json",
                producer_label="review-model",
            )
            self.assertEqual(invalid_path.name, "attempt-0001")
            self.assertEqual(invalid["validation"]["status"], "unusable")
            self.assertFalse(paths.derived_attempt_dir("attempt-0001").exists())

            result_value = self.results(paths)
            result_bytes = self.encoded(result_value)
            attempt_path, attempt = review.collect_review_results(
                workspace.root,
                result_bytes,
                review_id=paths.review_id,
                method="terminal-paste",
                source_name="pasted-check-results.json",
                producer_label="review-model",
            )
            self.assertEqual(attempt_path.name, "attempt-0002")
            self.assertEqual(attempt["validation"], {"status": "valid", "errors": []})
            self.assertEqual((attempt_path / "response.json").read_bytes(), result_bytes)

            derived = paths.derived_attempt_dir("attempt-0002")
            index = json.loads(
                (derived / "claim-review-index.json").read_text(encoding="utf-8")
            )
            self.assertEqual(contracts.validate_claim_review_index(index), [])
            self.assertEqual(index["summary"]["fail"], 1)
            self.assertEqual(index["summary"]["uncertain"], 1)
            self.assertGreater(index["summary"]["pass"], 0)
            self.assertEqual(len(list((derived / "findings").glob("F*.json"))), 2)
            first_finding = json.loads(
                (derived / "findings" / "F0001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(contracts.validate_argument_finding(first_finding), [])
            self.assertTrue(first_finding["target_claim"].startswith("V1:C"))
            self.assertEqual(first_finding["provenance"]["origin"], "model-derived")
            self.assertEqual(first_finding["status"], "open")

            plan = json.loads(paths.plan.read_text(encoding="utf-8"))
            target = plan["tasks"][0]["claim_id"]
            rendered, full_view = review.show_claim_review(
                workspace.root, review_id="RV1", claim_id=target
            )
            self.assertIn(f"## V1:{target}", rendered)
            self.assertIn(plan["tasks"][0]["check_id"], rendered)
            self.assertIn("[model-derived]", rendered)
            self.assertTrue(full_view.is_file())
            self.assertEqual(workbench.verify_workspace(workspace), [])

            first_files = {
                path.relative_to(derived).as_posix(): path.read_bytes()
                for path in derived.rglob("*")
                if path.is_file()
            }
            _, changed = review.rebuild_review_attempt(paths, "attempt-0002")
            self.assertFalse(changed)
            self.assertEqual(
                first_files,
                {
                    path.relative_to(derived).as_posix(): path.read_bytes()
                    for path in derived.rglob("*")
                    if path.is_file()
                },
            )

    def test_bundled_realistic_review_fixture_matches_deterministic_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            paths = self.prepare(workspace)
            _, attempt = review.collect_review_results(
                workspace.root,
                (FIXTURE / "review-results.json").read_bytes(),
                review_id=paths.review_id,
                method="file",
                source_name="review-results.json",
                producer_label="fixture-review-model",
            )
            self.assertEqual(attempt["validation"], {"status": "valid", "errors": []})
            markdown = (
                paths.derived_attempt_dir("attempt-0001") / "claim-review.md"
            ).read_text(encoding="utf-8")
            self.assertIn("descriptive.denominator", markdown)
            self.assertIn("no comparison denominator", markdown)
            self.assertIn("interpret.rival-reading", markdown)
            self.assertEqual(workbench.verify_workspace(workspace), [])

    def test_review_snapshots_survive_later_ir_corrections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            first = self.prepare(workspace)
            snapshot = first.target_ir.read_bytes()
            result_bytes = self.encoded(self.results(first))
            review.collect_review_results(
                workspace.root,
                result_bytes,
                review_id="RV1",
                method="file",
                source_name="results.json",
                producer_label="review-model",
            )
            workbench.append_correction(
                workspace.root,
                {
                    "kind": "update_node",
                    "target": "raw:C1",
                    "changes": {"uncertainty": "Human reviewed this extraction."},
                },
                reason="Clarify extraction uncertainty after review.",
            )
            workbench.rebuild_workspace(workspace.root)
            self.assertEqual(first.target_ir.read_bytes(), snapshot)
            self.assertEqual(workbench.verify_workspace(workspace), [])
            second, created = review.prepare_rule_review(
                workspace.root, RULES, depth="core"
            )
            self.assertTrue(created)
            self.assertEqual(second.review_id, "RV2")
            self.assertNotEqual(second.target_ir.read_bytes(), snapshot)
            _, selected_view = review.show_claim_review(
                workspace.root, review_id=None, claim_id=None
            )
            self.assertIn("RV1", selected_view.parts)
            self.assertEqual(workbench.verify_workspace(workspace), [])

    def test_tampered_derived_finding_is_detected_and_rebuildable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            paths = self.prepare(workspace)
            review.collect_review_results(
                workspace.root,
                self.encoded(self.results(paths)),
                review_id="RV1",
                method="file",
                source_name="results.json",
                producer_label="review-model",
            )
            finding_path = (
                paths.derived_attempt_dir("attempt-0001")
                / "findings"
                / "F0001.json"
            )
            finding = json.loads(finding_path.read_text(encoding="utf-8"))
            finding["reason"] = "Tampered model judgment."
            finding_path.write_text(
                json.dumps(finding, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(
                any(
                    "not reproducible" in error
                    for error in workbench.verify_workspace(workspace)
                )
            )
            _, changed = review.rebuild_review_attempt(paths, "attempt-0001")
            self.assertTrue(changed)
            self.assertEqual(workbench.verify_workspace(workspace), [])

    def test_review_cli_file_and_paste_workflows_preserve_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(
                    critic_runner.main(
                        ["ir", "review", "prepare", str(workspace.root)]
                    ),
                    0,
                )
            paths = review.selected_rule_review(workspace.root)
            results = self.results(paths)
            results_path = Path(temporary) / "model results.json"
            results_path.write_bytes(self.encoded(results))
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "review",
                            "collect",
                            str(workspace.root),
                            "--file",
                            str(results_path),
                            "--producer-label",
                            "file-model",
                        ]
                    ),
                    0,
                )
                claim_id = json.loads(paths.plan.read_text(encoding="utf-8"))["tasks"][0][
                    "claim_id"
                ]
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "review",
                            "show",
                            str(workspace.root),
                            "--claim",
                            claim_id,
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        ["ir", "verify-project", str(workspace.root)]
                    ),
                    0,
                )
            pasted = results_path.read_text(encoding="utf-8").rstrip() + "\n::END::\n"
            with patch("sys.stdin", StringIO(pasted)), redirect_stdout(
                stdout
            ), redirect_stderr(stderr):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "review",
                            "collect",
                            str(workspace.root),
                            "--paste",
                            "--producer-label",
                            "paste-model",
                        ]
                    ),
                    0,
                )
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("Validation status: valid", stdout.getvalue())
            self.assertTrue(paths.attempt_dir("attempt-0002").is_dir())
            self.assertEqual(workbench.verify_workspace(workspace), [])


if __name__ == "__main__":
    unittest.main()
