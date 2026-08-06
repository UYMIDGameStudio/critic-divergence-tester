from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import critic_runner  # noqa: E402


class CriticRunnerTests(unittest.TestCase):
    def test_strip_frontmatter_removes_metadata(self) -> None:
        source = "---\nname: example\n---\n\nBody\n"
        self.assertEqual(critic_runner.strip_frontmatter(source), "Body")

    def test_strip_frontmatter_rejects_unterminated_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "unterminated"):
            critic_runner.strip_frontmatter("---\nname: example\n")

    def test_generic_protocol_requires_explicit_unlock(self) -> None:
        with self.assertRaisesRegex(ValueError, "test artifact"):
            critic_runner.load_protocol("critic-generic")

        body, raw = critic_runner.load_protocol(
            "critic-generic", allow_test_artifact=True
        )
        self.assertIn("你审查一篇文章的论证", body)
        self.assertTrue(raw.startswith("---"))

    def test_prepare_archives_self_contained_prompt_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "draft.md"
            source.write_text("一份测试稿件。\n", encoding="utf-8")
            runs_dir = root / "runs"
            args = argparse.Namespace(
                protocol="critic-individualist",
                manuscript=str(source),
                runs_dir=str(runs_dir),
                allow_test_artifact=False,
            )

            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.prepare(args), 0)
            run_dir = next(runs_dir.iterdir())
            prompt = (run_dir / "prompt.md").read_text(encoding="utf-8")
            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )

            self.assertIn("# 审查协议", prompt)
            self.assertIn("一份测试稿件。", prompt)
            self.assertNotIn("name: critic-individualist", prompt)
            self.assertEqual(manifest["source_sha256"], critic_runner.sha256_text("一份测试稿件。\n"))
            self.assertEqual(manifest["prompt_sha256"], critic_runner.sha256_text(prompt))
            self.assertIsNone(manifest["executor"])

    def test_run_uses_utf8_and_does_not_archive_executor_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "稿件.md"
            source.write_text("中文输入。\n", encoding="utf-8")
            runs_dir = root / "runs"
            secret = "sk-not-a-real-secret"
            executor = [
                sys.executable,
                "-c",
                (
                    "import sys; data=sys.stdin.buffer.read().decode('utf-8'); "
                    "result='收到中文' if '中文输入' in data else 'missing'; "
                    "sys.stdout.buffer.write(result.encode('utf-8'))"
                ),
                secret,
            ]
            args = argparse.Namespace(
                protocol="critic-contrastivist",
                manuscript=str(source),
                runs_dir=str(runs_dir),
                allow_test_artifact=False,
                executor=executor,
            )

            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.run(args), 0)
            run_dir = next(runs_dir.iterdir())
            self.assertEqual(
                (run_dir / "report.md").read_text(encoding="utf-8").strip(),
                "收到中文",
            )
            manifest_text = (run_dir / "manifest.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertNotIn(secret, manifest_text)
            self.assertEqual(manifest["executor"]["command"], Path(sys.executable).name)
            self.assertEqual(manifest["executor"]["argument_count"], 3)
            self.assertEqual(manifest["returncode"], 0)

    def test_executor_start_failure_is_still_archived(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "draft.md"
            source.write_text("draft\n", encoding="utf-8")
            runs_dir = root / "runs"
            args = argparse.Namespace(
                protocol="critic-individualist",
                manuscript=str(source),
                runs_dir=str(runs_dir),
                allow_test_artifact=False,
                executor=[str(root / "missing-executor")],
            )

            with redirect_stderr(io.StringIO()):
                self.assertEqual(critic_runner.run(args), 2)
            run_dir = next(runs_dir.iterdir())
            self.assertTrue((run_dir / "prompt.md").is_file())
            self.assertTrue((run_dir / "manifest.json").is_file())
            self.assertTrue((run_dir / "stderr.log").is_file())
            self.assertFalse((run_dir / "report.md").exists())

    def test_interrupted_executor_keeps_inputs_archived(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "draft.md"
            source.write_text("draft\n", encoding="utf-8")
            runs_dir = root / "runs"
            args = argparse.Namespace(
                protocol="critic-individualist",
                manuscript=str(source),
                runs_dir=str(runs_dir),
                allow_test_artifact=False,
                executor=["executor"],
            )

            with patch.object(critic_runner.subprocess, "run", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    critic_runner.run(args)

            run_dir = next(runs_dir.iterdir())
            self.assertTrue((run_dir / "prompt.md").is_file())
            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["executor"]["command"], "executor")
            self.assertIsNone(manifest["returncode"])
            self.assertFalse((run_dir / "report.md").exists())


if __name__ == "__main__":
    unittest.main()
