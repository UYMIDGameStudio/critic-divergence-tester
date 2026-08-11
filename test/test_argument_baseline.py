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

import argument_baseline as baseline  # noqa: E402
import argument_contracts as contracts  # noqa: E402
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
                model_provider="Example Provider",
                model_id="example-model",
                interaction_mode="fresh-session",
                prior_context="none",
                manuscript_delivery="attachment",
                full_manuscript_confirmed=True,
                started_at="2026-08-10T10:00:00+08:00",
                completed_at="2026-08-10T10:03:12.500+08:00",
                producer_label="example-direct-model",
            )
            record = json.loads(paths.record.read_text(encoding="utf-8"))
            self.assertEqual(record["artifact"], "direct-review-baseline")
            self.assertEqual(record["schema_version"], 2)
            self.assertEqual(record["timing"]["elapsed_milliseconds"], 192500)
            self.assertEqual(record["conditions"]["interaction_mode"], "fresh-session")
            self.assertEqual(baseline.controlled_baseline_errors(record), [])
            legacy = copy.deepcopy(record)
            legacy["schema_version"] = 1
            legacy["model"] = {"label": legacy["model"]["label"]}
            del legacy["conditions"]
            legacy["field_provenance"] = {
                "source": legacy["field_provenance"]["source"],
                "prompt": legacy["field_provenance"]["prompt"],
                "response": legacy["field_provenance"]["response"],
                "model": legacy["field_provenance"]["model"],
                "timing": {
                    "origin": "deterministic",
                    "source": "supplied timestamps",
                },
            }
            self.assertEqual(contracts.validate_artifact(legacy), [])
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
                            "--model-provider",
                            "test-provider",
                            "--model-id",
                            "test-model-v1",
                            "--interaction-mode",
                            "fresh-session",
                            "--prior-context",
                            "none",
                            "--manuscript-delivery",
                            "attachment",
                            "--full-manuscript-confirmed",
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

    def test_prepare_baseline_embeds_exact_source_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            output = root / "direct prompt.md"
            path, digest = baseline.prepare_direct_review_prompt(project.root, output)
            version = json.loads(project.version.read_text(encoding="utf-8"))
            source = project.version_dir / version["source"]["relative_path"]
            prompt_bytes = path.read_bytes()
            self.assertTrue(prompt_bytes.startswith(baseline.DIRECT_REVIEW_HEADER))
            self.assertEqual(prompt_bytes.count(source.read_bytes()), 1)
            self.assertEqual(contracts.sha256_bytes(prompt_bytes), digest)
            with self.assertRaisesRegex(workbench.WorkbenchError, "refusing to overwrite"):
                baseline.prepare_direct_review_prompt(project.root, output)

            second = root / "cli prompt.md"
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "gate-a",
                            "prepare-baseline",
                            str(project.root),
                            str(second),
                        ]
                    ),
                    0,
                )
            self.assertIn("embedded verbatim", stdout.getvalue())

    def test_inline_collection_requires_exact_manuscript_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            prompt = root / "prompt.md"
            response = root / "response.md"
            prompt.write_text("This omits the manuscript.\n", encoding="utf-8")
            response.write_text("A model response.\n", encoding="utf-8")
            with self.assertRaisesRegex(
                workbench.WorkbenchError, "exact manuscript bytes"
            ):
                baseline.collect_direct_review_baseline(
                    project.root,
                    prompt_file=prompt,
                    response_file=response,
                    model_label="test-model",
                    model_provider="test-provider",
                    model_id="test-model-v1",
                    interaction_mode="fresh-session",
                    prior_context="none",
                    manuscript_delivery="inline",
                    full_manuscript_confirmed=True,
                    started_at="2026-08-10T10:00:00Z",
                    completed_at="2026-08-10T10:00:05Z",
                )

    def test_controlled_gate_baseline_rejects_contaminated_conditions(self) -> None:
        record = {
            "schema_version": 1,
        }
        self.assertIn("schema v2", baseline.controlled_baseline_errors(record)[0])
        record = {
            "schema_version": 2,
            "conditions": {
                "interaction_mode": "existing-session",
                "prior_context": "workbench-exposed",
                "manuscript_delivery": "attachment",
                "full_manuscript_confirmed": False,
            },
        }
        errors = baseline.controlled_baseline_errors(record)
        self.assertEqual(len(errors), 3)
        self.assertTrue(any("fresh session" in error for error in errors))
        self.assertTrue(any("no prior" in error for error in errors))
        self.assertTrue(any("full manuscript" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
