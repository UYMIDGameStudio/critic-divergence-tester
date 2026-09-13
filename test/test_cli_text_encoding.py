"""Legacy CLI source encodings, tested through actual command processes."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from cli.core import EXIT_INVALID_ARCHIVE, read_manuscript, read_manuscript_utf8
from test.test_critic_runner import VALID_REPORT


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "critic_runner.py"
MULTILINGUAL = (
    "# English · Original manuscript\n\n"
    "简体中文：天空湛蓝。繁體中文：花園寧靜。\n"
    "Deutsch: Grüße aus Köln; Äpfel und süße Früchte.\n"
    "Français : L’été, le cœur et l’œuvre.\n"
    "日本語：ひらがな、カタカナと静かな庭。\n"
    "Русский: Зелёные листья и ясное небо.\n"
    "Latīna: Cælum clārum; œconomia et rēs pūblica.\n"
)


class LegacyCLITextEncodingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="studio-cli-encoding-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.report = self.root / "report-fixture.md"
        self.report.write_bytes(VALID_REPORT.encode("utf-8"))
        self.captured = self.root / "captured-prompts.jsonl"
        self.executor = self.root / "executor-fixture.py"
        self.executor.write_text(
            "from pathlib import Path\n"
            "import base64, json, sys\n"
            "prompt = sys.stdin.buffer.read()\n"
            "prompt.decode('utf-8', errors='strict')\n"
            "with Path(sys.argv[2]).open('a', encoding='utf-8') as captured:\n"
            "    captured.write(json.dumps(base64.b64encode(prompt).decode('ascii')) + '\\n')\n"
            "if len(sys.argv) > 3:\n"
            "    Path(sys.argv[3]).write_bytes(b'MUTATED SOURCE DURING EXECUTION')\n"
            "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())\n",
            encoding="utf-8",
        )

    def command(self, *arguments, expected=0):
        result = subprocess.run(
            [sys.executable, str(RUNNER), *map(str, arguments)],
            cwd=self.root, capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"}, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def executor_args(self, mutate=None):
        result = ["--", sys.executable, self.executor, self.report, self.captured]
        if mutate is not None:
            result.append(mutate)
        return result

    def source(self, name, raw):
        path = self.root / name
        path.write_bytes(raw)
        return path

    def assert_archive(self, archive, source, raw, text, *, verify=True):
        manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
        prompt = (archive / "prompt.md").read_bytes()
        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["source_name"], source.name)
        self.assertEqual(manifest["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(manifest["prompt_sha256"], hashlib.sha256(prompt).hexdigest())
        self.assertIn(text.rstrip(), prompt.decode("utf-8"))
        self.assertNotIn("\ufffd", prompt.decode("utf-8"))
        if verify:
            self.command("verify-run", archive, "--source", source)
        return manifest

    def test_prepare_utf16_eight_languages_and_original_byte_verification(self):
        raw = MULTILINGUAL.encode("utf-16")
        source = self.source("八語 原稿.md", raw)
        runs = self.root / "runs"
        self.command("prepare", "critic-contrastivist", source, "--runs-dir", runs)
        archive = next(runs.iterdir())
        self.assert_archive(archive, source, raw, MULTILINGUAL)
        self.assertEqual(source.read_bytes(), raw)
        # The same displayed text with different source bytes is still a
        # different manuscript for the archive's original SHA binding.
        source.write_bytes(MULTILINGUAL.encode("utf-8"))
        result = self.command("verify-run", archive, "--source", source, expected=EXIT_INVALID_ARCHIVE)
        self.assertIn("source_sha256", result.stderr + result.stdout)

    def test_utf8_and_utf8_bom_keep_existing_archive_and_helper_contract(self):
        for encoding in ("utf-8", "utf-8-sig"):
            with self.subTest(encoding=encoding):
                raw = MULTILINGUAL.encode(encoding)
                source = self.source(encoding + ".md", raw)
                runs = self.root / encoding
                self.command("prepare", "critic-contrastivist", source, "--runs-dir", runs)
                self.assert_archive(next(runs.iterdir()), source, raw, MULTILINGUAL)
                self.assertEqual(read_manuscript_utf8(source), (MULTILINGUAL, raw))

    def test_quickstart_and_prepare_track_forward_explicit_legacy_encoding(self):
        text = "Русский: Зелёные листья и ясное небо.\n"
        raw = text.encode("cp1251")
        source = self.source("Русский manuscript.md", raw)
        cases = [
            ("quickstart", source, "--track", "natural-science"),
            ("prepare-track", "natural-science", source),
        ]
        for index, arguments in enumerate(cases):
            with self.subTest(command=arguments[0]):
                runs = self.root / ("manual-" + str(index))
                self.command(*arguments, "--encoding", "cp1251", "--runs-dir", runs)
                self.assert_archive(next(runs.iterdir()), source, raw, text)
        self.assertEqual(source.read_bytes(), raw)

    def test_run_and_run_track_use_utf8_transport_with_original_legacy_hash(self):
        text = "日本語：ひらがなとカタカナ、静かな庭。\n"
        raw = text.encode("cp932")
        source = self.source("日本語 manuscript.md", raw)
        cases = [
            ("run", "critic-contrastivist", source),
            ("run-track", "natural-science", source),
        ]
        for index, arguments in enumerate(cases):
            with self.subTest(command=arguments[0]):
                runs = self.root / ("executed-" + str(index))
                self.command(*arguments, "--encoding", "cp932", "--runs-dir", runs,
                             "--timeout", "15", *self.executor_args())
                archive = next(runs.iterdir())
                manifest = self.assert_archive(archive, source, raw, text)
                self.assertEqual(manifest["status"], "succeeded")
                self.assertEqual((archive / "report.md").read_bytes(), VALID_REPORT.encode("utf-8"))
        captured = [base64.b64decode(json.loads(line)) for line in self.captured.read_text().splitlines()]
        self.assertEqual(len(captured), 2)
        self.assertTrue(all(text.rstrip() in prompt.decode("utf-8") for prompt in captured))
        self.assertEqual(source.read_bytes(), raw)

    def test_campaign_freezes_legacy_source_when_file_changes_between_runs(self):
        text = "繁體中文：天空湛藍，花園寧靜。\n"
        raw = text.encode("big5")
        source = self.source("campaign manuscript.md", raw)
        campaigns = self.root / "campaigns"
        self.command("campaign", source, "--encoding", "big5", "--campaigns-dir", campaigns,
                     "--order-seed", "legacy-encoding", "--timeout", "15", *self.executor_args(source))
        archive = next(campaigns.iterdir())
        manifest = json.loads((archive / "campaign.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["runs"]), 4)
        self.assertEqual(manifest["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(source.read_bytes(), raw)
        for run in manifest["runs"]:
            child = self.assert_archive(archive / run["run_dir"], source, raw, text, verify=False)
            self.assertEqual(child["status"], "succeeded")
        captured = [base64.b64decode(json.loads(line)) for line in self.captured.read_text().splitlines()]
        self.assertEqual(len(captured), 4)
        self.assertTrue(all(text.rstrip() in prompt.decode("utf-8") for prompt in captured))
        source.write_bytes(raw)
        self.command("verify-campaign", archive, "--source", source)

    def test_ambiguous_auto_input_rejects_without_archive_then_explicit_retry_succeeds(self):
        text = "中文"
        raw = text.encode("big5")
        source = self.source("ambiguous.md", raw)
        cases = [
            (["prepare", "critic-contrastivist", source], "--runs-dir", []),
            (["quickstart", source, "--track", "natural-science"], "--runs-dir", []),
            (["run", "critic-contrastivist", source], "--runs-dir", self.executor_args()),
            (["campaign", source], "--campaigns-dir", self.executor_args()),
        ]
        for index, (arguments, output_option, executor) in enumerate(cases):
            with self.subTest(command=arguments[0]):
                output = self.root / ("ambiguous-" + str(index))
                rejected = self.command(*arguments, output_option, output, *executor, expected=2)
                self.assertIn("--encoding", rejected.stderr)
                self.assertIn("big5", rejected.stderr)
                self.assertFalse(output.exists())
        runs = self.root / "retried"
        self.command("prepare", "critic-contrastivist", source, "--encoding", "big5", "--runs-dir", runs)
        self.assert_archive(next(runs.iterdir()), source, raw, text)
        self.assertEqual(source.read_bytes(), raw)

    def test_wrong_unsupported_encoding_and_binary_are_refused_before_archive(self):
        source = self.source("legacy.md", "Русский текст".encode("cp1251"))
        for index, encoding in enumerate(("utf-8", "rot13", "not-an-encoding")):
            with self.subTest(encoding=encoding):
                output = self.root / ("invalid-" + str(index))
                result = self.command("prepare", "critic-contrastivist", source,
                                      "--encoding", encoding, "--runs-dir", output, expected=2)
                self.assertIn("--encoding", result.stderr)
                self.assertFalse(output.exists())
        binary = self.source("binary.txt", b"\x00\x01\x02")
        with self.assertRaises(ValueError):
            read_manuscript(binary)


if __name__ == "__main__":
    unittest.main()
