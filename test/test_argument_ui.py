from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import argument_adjudication as adjudication  # noqa: E402
import argument_review as review  # noqa: E402
import argument_ui as ui  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"
RULES = REPO_ROOT / "ir" / "social-science-checks.json"


class ArgumentUITests(unittest.TestCase):
    def make_project(self, root: Path):
        workspace = workbench.initialize_workspace(
            FIXTURE / "manuscript.md",
            root / "ui demo.argument-workbench",
            title="UI demo",
        )
        workbench.collect_raw_attempt(
            workspace,
            (FIXTURE / "raw-ir.json").read_bytes(),
            method="file",
            source_name="raw-ir.json",
            producer_label="fixture-extractor",
        )
        workbench.rebuild_workspace(workspace)
        review_paths, _ = review.prepare_rule_review(workspace, RULES, depth="core")
        review.collect_review_results(
            workspace,
            (FIXTURE / "review-results.json").read_bytes(),
            review_id=review_paths.review_id,
            method="file",
            source_name="review-results.json",
            producer_label="fixture-review-model",
        )
        return workspace

    def test_document_first_view_keeps_sources_lenses_and_human_state_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            view = ui.build_project_view(workspace)
            self.assertEqual(view["project"]["title"], "UI demo")
            self.assertEqual(view["project"]["version_id"], "V1")
            self.assertEqual(view["dashboard"]["claims"], 3)
            self.assertEqual(view["dashboard"]["open_findings"], 2)
            self.assertEqual(view["dashboard"]["unverified_citations"], 1)
            self.assertTrue(any(line["claim_ids"] for line in view["manuscript"]))
            claim = next(item for item in view["claims"] if item["id"] == "C1")
            self.assertTrue(any(item["from"] == "E2" for item in claim["incoming"]))
            self.assertTrue(any(item["id"] == "social-science" for item in view["lenses"]))
            self.assertEqual(
                view["provenance_legend"]["review_outcomes"], "model-derived"
            )
            self.assertNotIn("score", json.dumps(view).casefold())

    def test_ui_adjudication_uses_existing_append_only_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            finding_id = ui.build_project_view(workspace)["findings"][0]["finding_id"]
            ui.adjudicate_from_ui(
                workspace,
                {
                    "finding_id": finding_id,
                    "decision": "accept",
                    "reason": "The universal wording exceeds the evidence.",
                    "actions": [
                        {
                            "action_type": "narrow_claim",
                            "text": "Limit the Claim to the observed cases.",
                        }
                    ],
                },
            )
            view = ui.build_project_view(workspace)
            finding = next(item for item in view["findings"] if item["finding_id"] == finding_id)
            self.assertEqual(finding["decision"], "accept")
            self.assertEqual(finding["actions"][0]["action_type"], "narrow_claim")
            paths = adjudication.human_review_paths(workspace)
            decision = json.loads(
                next(paths.adjudications_dir.glob("AD*.json")).read_text(encoding="utf-8")
            )
            self.assertEqual(decision["provenance"]["origin"], "human-confirmed")
            self.assertEqual(decision["provenance"]["producer"], "local-workbench-ui")
            self.assertEqual(workbench.verify_project_versions(workspace), [])

    def test_stale_review_snapshot_is_not_presented_as_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            workbench.append_correction(
                workspace,
                {
                    "kind": "update_node",
                    "target": "raw:C1",
                    "changes": {"uncertainty": "Author has not confirmed the universal scope."},
                },
                reason="The reviewed Claim needs a human uncertainty marker.",
            )
            workbench.rebuild_workspace(workspace)
            view = ui.build_project_view(workspace)
            self.assertEqual(view["lenses"], [])
            self.assertEqual(view["outcomes"], [])
            self.assertEqual(view["findings"], [])
            self.assertEqual(view["dashboard"]["open_findings"], 0)
            self.assertTrue((workspace.version_dir / "reviews" / "RV1").is_dir())

    def test_http_api_requires_unpredictable_local_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            server, url = ui.serve_workbench(workspace, open_browser=False)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                try:
                    with urllib.request.urlopen(url, timeout=5) as response:
                        shell = response.read().decode("utf-8")
                except urllib.error.URLError as exc:
                    if isinstance(exc.reason, PermissionError):
                        self.skipTest("local TCP connections are blocked by this sandbox")
                    raise
                self.assertIn("Manuscript · 原文", shell)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(url + "api/view", timeout=5)
                self.assertEqual(caught.exception.code, 403)
                request = urllib.request.Request(
                    url + "api/view",
                    headers={"X-Argument-Workbench-Token": server.app.token},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    view = json.loads(response.read())
                self.assertEqual(view["project"]["version_id"], "V1")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_non_loopback_listener_is_refused_and_cli_help_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_project(Path(temporary))
            with self.assertRaisesRegex(workbench.WorkbenchError, "loopback"):
                ui.serve_workbench(
                    workspace, host="0.0.0.0", open_browser=False
                )
        parser = critic_runner.parser()
        args = parser.parse_args(["ir", "ui", "demo", "--no-browser"])
        self.assertIs(args.func, critic_runner.ir_ui_command)


if __name__ == "__main__":
    unittest.main()
