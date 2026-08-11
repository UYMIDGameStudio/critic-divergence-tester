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

import argument_baseline as baseline  # noqa: E402
import argument_workbench as workbench  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"


class DirectReviewBaselineTests(unittest.TestCase):
    def make_project(self, root: Path) -> workbench.WorkspacePaths:
        paths = workbench.initialize_workspace(
            FIXTURE / "manuscript.md",
            root / "baseline demo.argument-workbench",
            title="Direct baseline demo",
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

    def test_exact_prompt_response_model_and_time_are_immutable_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            prompt = root / "Direct prompt.md"
            response = root / "Direct response.md"
            prompt.write_bytes("请直接审查这篇完整稿件。\r\n".encode("utf-8"))
            response.write_bytes("模型的原始回答。\r\n".encode("utf-8"))
            paths = baseline.collect_direct_review_baseline(
                project.root,
                prompt_file=prompt,
                response_file=response,
                model_label="example-model-2026-08",
                started_at="2026-08-10T10:00:00+08:00",
                completed_at="2026-08-10T10:03:12.500+08:00",
                producer_label="example-direct-model",
            )
            record = json.loads(paths.record.read_text(encoding="utf-8"))
            self.assertEqual(record["artifact"], "direct-review-baseline")
            self.assertEqual(record["timing"]["elapsed_milliseconds"], 192500)
            self.assertEqual(paths.prompt.read_bytes(), prompt.read_bytes())
            self.assertEqual(paths.response.read_bytes(), response.read_bytes())
            self.assertEqual(baseline.verify_direct_review_baselines(project.root), [])
            self.assertEqual(workbench.verify_workspace(project.root), [])

            paths.response.write_text("tampered\n", encoding="utf-8")
            self.assertTrue(
                any(
                    "response exact-byte hash" in error
                    for error in baseline.verify_direct_review_baselines(project.root)
                )
            )

    def test_cli_collects_without_making_a_comparison_or_gate_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            prompt = root / "prompt.md"
            response = root / "response.md"
            prompt.write_text("Review the complete manuscript.\n", encoding="utf-8")
            response.write_text("Direct model response.\n", encoding="utf-8")
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "gate-a",
                            "baseline",
                            str(project.root),
                            "--prompt-file",
                            str(prompt),
                            "--response-file",
                            str(response),
                            "--model-label",
                            "test-model",
                            "--started-at",
                            "2026-08-10T10:00:00Z",
                            "--completed-at",
                            "2026-08-10T10:00:05Z",
                        ]
                    ),
                    0,
                )
            self.assertIn("No comparison or Gate decision was made", stdout.getvalue())
            entries = baseline.list_direct_review_baselines(project.root)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0][1]["provenance"]["origin"], "model-derived")


if __name__ == "__main__":
    unittest.main()
