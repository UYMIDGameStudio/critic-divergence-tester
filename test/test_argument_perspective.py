from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_contracts as contracts  # noqa: E402
import argument_adjudication as adjudication  # noqa: E402
import argument_lens_view as lens_view  # noqa: E402
import argument_perspective as perspective  # noqa: E402
import argument_review as rule_review  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"


class PerspectiveReviewTests(unittest.TestCase):
    def make_project(self, root: Path) -> workbench.WorkspacePaths:
        paths = workbench.initialize_workspace(
            FIXTURE / "manuscript.md",
            root / "perspective demo.argument-workbench",
            title="Perspective review demo",
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

    @staticmethod
    def encoded(value: object) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )

    def results(
        self,
        paths: perspective.PerspectiveReviewPaths,
    ) -> dict[str, object]:
        plan = json.loads(paths.plan.read_text(encoding="utf-8"))
        selected = plan["review_scope"]["selected_claim_ids"]
        items = []
        for index, claim_id in enumerate(selected):
            verdict = "fail" if index == 0 else "pass"
            items.append(
                {
                    "result_id": f"P{index + 1}",
                    "target_claim": claim_id,
                    "verdict": verdict,
                    "reason": "The explanation stops at an aggregate category."
                    if verdict == "fail"
                    else "The Claim reconstructs the relevant actor-level transition.",
                    "basis_refs": [claim_id],
                    "framework_analysis": (
                        "Applying the complete methodological-individualist framework, "
                        "the causal bearer is not identified."
                        if verdict == "fail"
                        else "The complete framework finds an actor-level mechanism."
                    ),
                    "consequence": "Add an actor-level mechanism."
                    if verdict == "fail"
                    else "",
                }
            )
        return {
            "schema_version": 1,
            "artifact": "perspective-lens-results",
            "source": {
                "plan_sha256": contracts.sha256_bytes(paths.plan.read_bytes()),
                "target_ir_sha256": contracts.sha256_bytes(
                    paths.target_ir.read_bytes()
                ),
                "protocol_sha256": contracts.sha256_bytes(
                    paths.protocol.read_bytes()
                ),
            },
            "status": "complete",
            "unverified": [],
            "results": items,
        }

    def test_prepare_collect_normalize_and_verify_holistic_lens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            paths, created = perspective.prepare_perspective_review(
                workspace.root,
                lens_id="methodological-individualism",
                review_scope="thesis-chain",
            )
            self.assertTrue(created)
            self.assertEqual(paths.review_id, "PV1")
            self.assertEqual(
                paths.protocol.read_bytes(),
                (REPO_ROOT / "critic-individualist.md").read_bytes(),
            )
            prompt = paths.prompt.read_text(encoding="utf-8")
            self.assertIn("complete framework", prompt)
            self.assertIn("Do not turn it into a checklist", prompt)

            repeated, created = perspective.prepare_perspective_review(
                workspace.root,
                lens_id="methodological-individualism",
                review_scope="thesis-chain",
            )
            self.assertFalse(created)
            self.assertEqual(repeated.root, paths.root)

            invalid_dir, invalid = perspective.collect_perspective_results(
                workspace.root,
                b'{"artifact":"x","artifact":"duplicate"}\n',
                review_id="PV1",
                method="file",
                source_name="invalid.json",
                producer_label="perspective-model",
            )
            self.assertEqual(invalid_dir.name, "attempt-0001")
            self.assertEqual(invalid["validation"]["status"], "unusable")
            self.assertFalse(paths.derived_attempt_dir("attempt-0001").exists())

            response = self.encoded(self.results(paths))
            attempt_dir, attempt = perspective.collect_perspective_results(
                workspace.root,
                response,
                review_id="PV1",
                method="terminal-paste",
                source_name="pasted-perspective-results.json",
                producer_label="perspective-model",
            )
            self.assertEqual(attempt_dir.name, "attempt-0002")
            self.assertEqual(attempt["validation"], {"status": "valid", "errors": []})
            derived = paths.derived_attempt_dir("attempt-0002")
            index = json.loads(
                (derived / "perspective-review-index.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(contracts.validate_perspective_review_index(index), [])
            self.assertEqual(index["summary"]["fail"], 1)
            self.assertGreater(index["summary"]["pass"], 0)
            findings = list((derived / "findings").glob("F*.json"))
            self.assertEqual(len(findings), 1)
            finding = json.loads(findings[0].read_text(encoding="utf-8"))
            self.assertEqual(finding["lens"]["kind"], "perspective")
            self.assertIsNone(finding["lens"]["check_id"])
            self.assertEqual(
                finding["parents"][1]["artifact"], "perspective-lens-results"
            )
            current = adjudication.current_finding_entries(workspace.root)
            self.assertEqual([entry.value["finding_id"] for entry in current], [finding["finding_id"]])
            decision_path, actions = adjudication.append_finding_decision(
                workspace.root,
                finding["finding_id"],
                decision="reject",
                reason="The author rejects this framework-specific objection.",
                actions=[],
            )
            self.assertTrue(decision_path.is_file())
            self.assertEqual(actions, [])

            later, _ = perspective.prepare_perspective_review(
                workspace.root,
                lens_id="contrastive-explanation",
                review_scope="claim",
                claim_ids=["C1"],
            )
            later_result = self.results(later)
            later_result["results"][0].update(
                verdict="pass",
                reason="The relevant contrast is explicit.",
                framework_analysis="The complete framework identifies the stated foil.",
                consequence="",
            )
            perspective.collect_perspective_results(
                workspace.root,
                self.encoded(later_result),
                review_id=later.review_id,
                method="file",
                source_name="later-result.json",
                producer_label="perspective-model",
            )

            rendered, view = perspective.show_perspective_review(
                workspace.root,
                review_id="PV1",
                claim_id=finding["target_claim"],
            )
            self.assertIn("Perspective Lens", rendered)
            self.assertIn("no vote or cross-lens synthesis", rendered)
            self.assertTrue(view.is_file())
            self.assertEqual(perspective.verify_perspective_reviews(workspace.root), [])
            self.assertEqual(workbench.verify_workspace(workspace.root), [])

            before = {
                path.relative_to(derived).as_posix(): path.read_bytes()
                for path in derived.rglob("*")
                if path.is_file()
            }
            _, changed = perspective.rebuild_perspective_attempt(
                paths, "attempt-0002"
            )
            self.assertFalse(changed)
            self.assertEqual(
                before,
                {
                    path.relative_to(derived).as_posix(): path.read_bytes()
                    for path in derived.rglob("*")
                    if path.is_file()
                },
            )

    def test_contrastive_protocol_and_scope_are_distinct_not_votes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            paths, created = perspective.prepare_perspective_review(
                workspace.root,
                lens_id="contrastive-explanation",
                review_scope="claim",
                claim_ids=["C2"],
            )
            self.assertTrue(created)
            plan = json.loads(paths.plan.read_text(encoding="utf-8"))
            self.assertEqual(plan["review_scope"]["selected_claim_ids"], ["C2"])
            self.assertEqual(
                paths.protocol.read_bytes(),
                (REPO_ROOT / "critic-contrastivist.md").read_bytes(),
            )
            self.assertNotIn("overall confidence", paths.prompt.read_text(encoding="utf-8").lower())

    def test_perspective_cli_prepare_collect_show_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "review",
                            "prepare-perspective",
                            str(workspace.root),
                            "--lens",
                            "contrastive-explanation",
                            "--scope",
                            "claim",
                            "--claim",
                            "C2",
                        ]
                    ),
                    0,
                )
            paths = perspective.selected_perspective_review(workspace.root)
            response_path = Path(temporary) / "perspective results.json"
            response_path.write_bytes(self.encoded(self.results(paths)))
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "review",
                            "collect-perspective",
                            str(workspace.root),
                            "--file",
                            str(response_path),
                            "--producer-label",
                            "cli-model",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "review",
                            "show-perspective",
                            str(workspace.root),
                            "--claim",
                            "C2",
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
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("Perspective Review: PV1", stdout.getvalue())
            self.assertIn("Validation status: valid", stdout.getvalue())

    def test_claim_lens_view_preserves_rule_and_perspective_disagreement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            rule_paths, _ = rule_review.prepare_rule_review(
                workspace.root,
                REPO_ROOT / "ir" / "social-science-checks.json",
                depth="core",
            )
            rule_review.collect_review_results(
                workspace.root,
                (FIXTURE / "review-results.json").read_bytes(),
                review_id=rule_paths.review_id,
                method="file",
                source_name="review-results.json",
                producer_label="rule-model",
            )

            individualist, _ = perspective.prepare_perspective_review(
                workspace.root,
                lens_id="methodological-individualism",
                review_scope="claim",
                claim_ids=["C1"],
            )
            perspective.collect_perspective_results(
                workspace.root,
                self.encoded(self.results(individualist)),
                review_id=individualist.review_id,
                method="file",
                source_name="individualist.json",
                producer_label="perspective-model",
            )

            contrastive, _ = perspective.prepare_perspective_review(
                workspace.root,
                lens_id="contrastive-explanation",
                review_scope="claim",
                claim_ids=["C1"],
            )
            contrastive_result = self.results(contrastive)
            contrastive_result["results"][0].update(
                verdict="pass",
                reason="The Claim states the relevant contrast class.",
                framework_analysis="The complete contrastive framework finds the foil explicit.",
                consequence="",
            )
            perspective.collect_perspective_results(
                workspace.root,
                self.encoded(contrastive_result),
                review_id=contrastive.review_id,
                method="file",
                source_name="contrastive.json",
                producer_label="perspective-model",
            )

            rendered = lens_view.render_claim_lenses(workspace.root, "C1")
            self.assertIn("social-science — Rule Lens", rendered)
            self.assertIn("methodological-individualism — Perspective Lens", rendered)
            self.assertIn("contrastive-explanation — Perspective Lens", rendered)
            self.assertIn("### FAIL", rendered)
            self.assertIn("### PASS", rendered)
            self.assertIn("No vote, average, winner", rendered)

            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "review",
                            "show-claim-lenses",
                            str(workspace.root),
                            "--claim",
                            "C1",
                        ]
                    ),
                    0,
                )
            self.assertIn("Review Lenses — V1:C1", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
