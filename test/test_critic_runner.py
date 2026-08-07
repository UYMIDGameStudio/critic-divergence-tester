from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import critic_runner  # noqa: E402


VALID_REPORT = """## 1. 原子指控
### A1
位置：“原文”
指控：缺少关键一步。
理由：理由。

## 2. 逐条后果检验
### A1
检验：补足后的论证。
结论：缩窄。

## 3. 核心论证压力测试
核心测试结果。

## 4. 唯一最弱一步
位置：“原文”
理由：最弱理由。

## 5. 唯一最强论证
位置：“另一处原文”
理由：最强理由。

## 6. 让步条件
出现反证时让步。

STATUS: complete
UNVERIFIED: none
"""

EMPTY_VALID_REPORT = """## 1. 原子指控
无实质异议。理由：论证成立。

## 2. 逐条后果检验
不适用：第一节没有原子指控。

## 3. 核心论证压力测试
核心论证存活。

## 4. 唯一最弱一步
不适用：没有最弱项。

## 5. 唯一最强论证
位置：“原文”
理由：论证成立。

## 6. 让步条件
不适用：没有最弱指控。

STATUS: complete
UNVERIFIED: none
"""

VALID_CITATION_REPORT = """## C1
文献：Example
稿件位置：“原文观点归属”
书目证据：A
书目来源：https://example.com/catalog
内容证据：A
内容来源：https://example.com/text#page-1
核对版本：First edition
定位：page 1
存在性：通过
书目：通过
观点：明确支持
语境：通过
问题：none

STATUS: complete
书目证据分布: A 1 / B 0 / C 0 / D 0
内容证据分布: A 1 / B 0 / C 0 / D 0
UNVERIFIED: none
"""


class CriticRunnerTests(unittest.TestCase):
    def _args(
        self,
        *,
        root: Path,
        executor: list[str] | None = None,
        timeout: float | None = None,
        protocol: str = "critic-individualist",
    ) -> argparse.Namespace:
        source = root / "稿件.md"
        if not source.exists():
            source.write_text("中文输入。\n", encoding="utf-8", newline="\n")
        return argparse.Namespace(
            protocol=protocol,
            manuscript=str(source),
            runs_dir=str(root / "runs"),
            allow_test_artifact=False,
            executor=executor,
            timeout=timeout,
        )

    def test_strip_frontmatter_removes_metadata_and_bom(self) -> None:
        source = "\ufeff---\nname: example\n---\n\nBody\n"
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
        self.assertTrue(raw.startswith(b"---"))

    def test_atomic_write_replaces_bytes_without_leaving_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "artifact"
            critic_runner.atomic_write_bytes(target, b"first")
            critic_runner.atomic_write_bytes(target, b"second")
            self.assertEqual(target.read_bytes(), b"second")
            self.assertEqual(list(target.parent.glob(".*.tmp")), [])

    def test_prepare_hashes_exact_file_bytes_and_removes_source_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "draft.md"
            source_raw = b"\xef\xbb\xbfline one\r\nline two\r\n"
            source.write_bytes(source_raw)
            args = argparse.Namespace(
                protocol="critic-individualist",
                manuscript=str(source),
                runs_dir=str(root / "runs"),
                allow_test_artifact=False,
            )

            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.prepare(args), 0)
            run_dir = next((root / "runs").iterdir())
            prompt_bytes = (run_dir / "prompt.md").read_bytes()
            prompt = prompt_bytes.decode("utf-8")
            manifest_text = (run_dir / "manifest.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)

            self.assertIn("line one\r\nline two", prompt)
            self.assertNotIn("\ufeff", prompt)
            self.assertEqual(
                manifest["source_sha256"], hashlib.sha256(source_raw).hexdigest()
            )
            self.assertEqual(
                manifest["prompt_sha256"], hashlib.sha256(prompt_bytes).hexdigest()
            )
            self.assertEqual(
                manifest["protocol_sha256"],
                hashlib.sha256(
                    critic_runner.PROTOCOLS["critic-individualist"].read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(manifest["source_name"], "draft.md")
            self.assertNotIn(str(root), manifest_text)
            self.assertEqual(manifest["status"], "prepared")
            self.assertEqual(manifest["started_at"], manifest["completed_at"])
            self.assertEqual(manifest["runner_exit_code"], 0)
            verification = critic_runner.verify_run_dir(run_dir, source)
            self.assertTrue(verification.valid, verification.errors)
            self.assertEqual(verification.warnings, ())

    def test_verify_run_detects_tampered_artifact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.prepare(args), 0)
            run_dir = next((root / "runs").iterdir())
            with (run_dir / "prompt.md").open("ab") as handle:
                handle.write(b"tampered")

            verification = critic_runner.verify_run_dir(
                run_dir, Path(args.manuscript)
            )
            self.assertFalse(verification.valid)
            self.assertTrue(any("hash mismatch" in error for error in verification.errors))

    def test_verify_run_detects_inconsistent_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.prepare(args), 0)
            run_dir = next((root / "runs").iterdir())
            manifest_path = run_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "succeeded"
            manifest_path.write_text(
                json.dumps(manifest), encoding="utf-8", newline="\n"
            )

            verification = critic_runner.verify_run_dir(run_dir)
            self.assertFalse(verification.valid)
            self.assertTrue(any("report_sha256" in error for error in verification.errors))
            self.assertTrue(any("executor" in error for error in verification.errors))

    def test_verify_run_rejects_reversed_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.prepare(args), 0)
            run_dir = next((root / "runs").iterdir())
            manifest_path = run_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["started_at"] = "2026-08-07T01:00:00+00:00"
            manifest["completed_at"] = "2026-08-07T00:59:59+00:00"
            manifest_path.write_text(
                json.dumps(manifest), encoding="utf-8", newline="\n"
            )

            verification = critic_runner.verify_run_dir(run_dir)
            self.assertFalse(verification.valid)
            self.assertTrue(any("earlier" in error for error in verification.errors))

    def test_verify_run_rejects_unreadable_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "manifest.json").write_text("{", encoding="utf-8")
            verification = critic_runner.verify_run_dir(run_dir)
            self.assertFalse(verification.valid)
            self.assertTrue(any("cannot read" in error for error in verification.errors))

    def test_verify_run_rechecks_supplied_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.prepare(args), 0)
            run_dir = next((root / "runs").iterdir())
            wrong_source = root / "other.md"
            wrong_source.write_text("different", encoding="utf-8")

            verification = critic_runner.verify_run_dir(run_dir, wrong_source)
            self.assertFalse(verification.valid)
            self.assertTrue(any("source name mismatch" in error for error in verification.errors))
            self.assertTrue(any("source bytes" in error for error in verification.errors))

    def test_verify_run_warns_when_current_protocol_has_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.prepare(args), 0)
            run_dir = next((root / "runs").iterdir())
            changed_protocol = root / "changed-protocol.md"
            changed_protocol.write_text("changed", encoding="utf-8")

            with patch.dict(
                critic_runner.PROTOCOLS,
                {"critic-individualist": changed_protocol},
            ):
                verification = critic_runner.verify_run_dir(
                    run_dir, Path(args.manuscript)
                )
            self.assertTrue(verification.valid, verification.errors)
            self.assertTrue(any("differs" in warning for warning in verification.warnings))

    def test_valid_critic_report_passes(self) -> None:
        result = critic_runner.validate_report("critic-individualist", VALID_REPORT)
        self.assertTrue(result.valid, result.errors)

    def test_valid_empty_critic_report_passes(self) -> None:
        result = critic_runner.validate_report(
            "critic-contrastivist", EMPTY_VALID_REPORT
        )
        self.assertTrue(result.valid, result.errors)

    def test_valid_citation_report_passes_evidence_checks(self) -> None:
        result = critic_runner.validate_report(
            "citation-auditor", VALID_CITATION_REPORT
        )
        self.assertTrue(result.valid, result.errors)

    def test_empty_citation_report_with_zero_distributions_passes(self) -> None:
        report = """无引证。

STATUS: complete
书目证据分布: A 0 / B 0 / C 0 / D 0
内容证据分布: A 0 / B 0 / C 0 / D 0
UNVERIFIED: none
"""
        result = critic_runner.validate_report("citation-auditor", report)
        self.assertTrue(result.valid, result.errors)

    def test_citation_validator_enforces_content_grade_interlocks(self) -> None:
        report = VALID_CITATION_REPORT.replace(
            "内容证据：A", "内容证据：B"
        ).replace(
            "内容证据分布: A 1 / B 0 / C 0 / D 0",
            "内容证据分布: A 0 / B 1 / C 0 / D 0",
        )
        result = critic_runner.validate_report("citation-auditor", report)
        self.assertFalse(result.valid)
        self.assertTrue(any("requires 语境" in error for error in result.errors))
        self.assertTrue(any("requires 观点" in error for error in result.errors))

    def test_citation_validator_rejects_incorrect_distributions(self) -> None:
        report = VALID_CITATION_REPORT.replace(
            "书目证据分布: A 1 / B 0 / C 0 / D 0",
            "书目证据分布: A 0 / B 1 / C 0 / D 0",
        )
        result = critic_runner.validate_report("citation-auditor", report)
        self.assertFalse(result.valid)
        self.assertTrue(
            any("distribution does not match" in error for error in result.errors)
        )

    def test_citation_validator_rejects_malformed_contract_variants(self) -> None:
        cases = {
            "missing field": (
                VALID_CITATION_REPORT.replace("文献：Example\n", ""),
                "文献： field",
            ),
            "empty field": (
                VALID_CITATION_REPORT.replace("文献：Example", "文献："),
                "empty 文献：",
            ),
            "malformed id": (
                VALID_CITATION_REPORT.replace("## C1", "## C0"),
                "malformed citation heading",
            ),
            "noncontinuous id": (
                VALID_CITATION_REPORT.replace("## C1", "## C2"),
                "continuous C1",
            ),
            "unexpected heading": (
                "## Summary\n" + VALID_CITATION_REPORT,
                "unexpected level-2",
            ),
            "invalid bibliography grade": (
                VALID_CITATION_REPORT.replace("书目证据：A", "书目证据：E"),
                "书目证据 must be",
            ),
            "invalid content grade": (
                VALID_CITATION_REPORT.replace("内容证据：A", "内容证据：E"),
                "内容证据 must be",
            ),
            "invalid existence verdict": (
                VALID_CITATION_REPORT.replace("存在性：通过", "存在性：不存在"),
                "invalid 存在性",
            ),
            "invalid bibliography verdict": (
                VALID_CITATION_REPORT.replace("书目：通过", "书目：未知"),
                "invalid 书目",
            ),
            "invalid viewpoint verdict": (
                VALID_CITATION_REPORT.replace("观点：明确支持", "观点：大概支持"),
                "invalid 观点",
            ),
            "invalid context verdict": (
                VALID_CITATION_REPORT.replace("语境：通过", "语境：未知"),
                "invalid 语境",
            ),
            "bibliography D existence interlock": (
                VALID_CITATION_REPORT.replace("书目证据：A", "书目证据：D")
                .replace(
                    "书目证据分布: A 1 / B 0 / C 0 / D 0",
                    "书目证据分布: A 0 / B 0 / C 0 / D 1",
                ),
                "grade D requires 存在性",
            ),
            "A bibliography source missing": (
                VALID_CITATION_REPORT.replace(
                    "书目来源：https://example.com/catalog", "书目来源：none"
                ),
                "grade A requires a concrete source",
            ),
            "A content source missing": (
                VALID_CITATION_REPORT.replace(
                    "内容来源：https://example.com/text#page-1", "内容来源：none"
                ),
                "content grade A requires a concrete source",
            ),
            "problem explanation missing": (
                VALID_CITATION_REPORT.replace("存在性：通过", "存在性：无法确认"),
                "requires a concrete 问题",
            ),
            "invalid distribution syntax": (
                VALID_CITATION_REPORT.replace(
                    "书目证据分布: A 1 / B 0 / C 0 / D 0",
                    "书目证据分布: A=1",
                ),
                "invalid bibliography evidence distribution",
            ),
            "missing no-citation declaration": (
                """STATUS: complete
书目证据分布: A 0 / B 0 / C 0 / D 0
内容证据分布: A 0 / B 0 / C 0 / D 0
UNVERIFIED: none
""",
                "explicit 无引证",
            ),
        }
        for name, (report, expected_error) in cases.items():
            with self.subTest(case=name):
                result = critic_runner.validate_report("citation-auditor", report)
                self.assertFalse(result.valid)
                self.assertTrue(
                    any(expected_error in error for error in result.errors),
                    result.errors,
                )

    def test_all_critic_protocols_embed_the_same_output_headings(self) -> None:
        for protocol_name in critic_runner.CRITIC_PROTOCOLS:
            with self.subTest(protocol=protocol_name):
                body, _ = critic_runner.load_protocol(
                    protocol_name,
                    allow_test_artifact=protocol_name in critic_runner.TEST_ONLY,
                )
                positions = [body.index(heading) for heading in critic_runner.CRITIC_SECTIONS]
                self.assertEqual(positions, sorted(positions))
                for heading in critic_runner.CRITIC_SECTIONS:
                    self.assertEqual(body.count(heading), 1)

    def test_validator_rejects_missing_footer(self) -> None:
        result = critic_runner.validate_report(
            "critic-individualist", VALID_REPORT.rsplit("STATUS:", 1)[0]
        )
        self.assertFalse(result.valid)
        self.assertTrue(any("STATUS" in error for error in result.errors))

    def test_validator_rejects_duplicate_footer_fields(self) -> None:
        report = VALID_REPORT.replace(
            "STATUS: complete", "STATUS: partial\nSTATUS: complete"
        )
        result = critic_runner.validate_report("critic-individualist", report)
        self.assertFalse(result.valid)
        self.assertTrue(any("exactly one valid STATUS" in error for error in result.errors))

    def test_validator_enforces_status_and_unverified_consistency(self) -> None:
        cases = (
            VALID_REPORT.replace("UNVERIFIED: none", "UNVERIFIED: A1 location"),
            VALID_REPORT.replace("STATUS: complete", "STATUS: partial"),
            VALID_REPORT.replace("STATUS: complete", "STATUS: blocked"),
        )
        for report in cases:
            with self.subTest(footer=report.splitlines()[-2:]):
                result = critic_runner.validate_report("critic-individualist", report)
                self.assertFalse(result.valid)
                self.assertTrue(any("UNVERIFIED" in error for error in result.errors))

    def test_validator_rejects_reordered_sections(self) -> None:
        report = VALID_REPORT.replace(
            "## 1. 原子指控", "## TEMP", 1
        ).replace(
            "## 2. 逐条后果检验", "## 1. 原子指控", 1
        ).replace("## TEMP", "## 2. 逐条后果检验", 1)
        result = critic_runner.validate_report("critic-individualist", report)
        self.assertFalse(result.valid)
        self.assertTrue(any("headings" in error for error in result.errors))

    def test_validator_rejects_noncontinuous_and_unmatched_items(self) -> None:
        report = VALID_REPORT.replace("### A1", "### A2", 1)
        result = critic_runner.validate_report("critic-individualist", report)
        self.assertFalse(result.valid)
        self.assertTrue(any("continuous" in error for error in result.errors))
        self.assertTrue(any("matching entry" in error for error in result.errors))

    def test_validator_rejects_missing_atomic_item_fields(self) -> None:
        report = VALID_REPORT.replace("指控：缺少关键一步。\n", "")
        result = critic_runner.validate_report("critic-individualist", report)
        self.assertFalse(result.valid)
        self.assertTrue(any("指控： field" in error for error in result.errors))

    def test_validator_rejects_empty_report_with_nonapplicable_section_missing(self) -> None:
        report = EMPTY_VALID_REPORT.replace(
            "不适用：第一节没有原子指控。", "没有逐条检验。"
        )
        result = critic_runner.validate_report("critic-individualist", report)
        self.assertFalse(result.valid)
        self.assertTrue(any("explicitly say 不适用" in error for error in result.errors))

    def test_validator_rejects_nonapplicable_weakest_item_when_claims_exist(self) -> None:
        report = VALID_REPORT.replace(
            "## 4. 唯一最弱一步\n位置：“原文”",
            "## 4. 唯一最弱一步\n不适用：没有最弱项",
        )
        result = critic_runner.validate_report("critic-individualist", report)
        self.assertFalse(result.valid)
        self.assertTrue(any("identify one 位置" in error for error in result.errors))

    def test_critic_validator_rejects_malformed_contract_variants(self) -> None:
        cases = {
            "malformed atomic id": (
                VALID_REPORT.replace("### A1", "### A0", 1),
                "malformed atomic-item",
            ),
            "empty atomic field": (
                VALID_REPORT.replace("理由：理由。", "理由："),
                "empty 理由：",
            ),
            "no items and no empty conclusion": (
                EMPTY_VALID_REPORT.replace("无实质异议。理由：论证成立。", "没有条目。"),
                "explicit 无实质异议",
            ),
            "empty required section": (
                VALID_REPORT.replace(
                    "## 3. 核心论证压力测试\n核心测试结果。",
                    "## 3. 核心论证压力测试",
                ),
                "must not be empty",
            ),
            "missing weakest marker": (
                VALID_REPORT.replace("位置：“原文”\n理由：最弱理由。", "理由：最弱理由。"),
                "section 4 must contain exactly one",
            ),
            "missing strongest marker": (
                VALID_REPORT.replace(
                    "位置：“另一处原文”\n理由：最强理由。", "理由：最强理由。"
                ),
                "section 5 must contain exactly one",
            ),
            "empty strongest reason": (
                VALID_REPORT.replace("理由：最强理由。", "理由："),
                "section 5 must contain exactly one non-empty",
            ),
            "empty unverified": (
                VALID_REPORT.replace("UNVERIFIED: none", "UNVERIFIED:"),
                "UNVERIFIED must contain a value",
            ),
        }
        for name, (report, expected_error) in cases.items():
            with self.subTest(case=name):
                result = critic_runner.validate_report(
                    "critic-individualist", report
                )
                self.assertFalse(result.valid)
                self.assertTrue(
                    any(expected_error in error for error in result.errors),
                    result.errors,
                )

    def test_run_uses_utf8_redacts_arguments_and_records_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            secret = "sk-not-a-real-secret"
            encoded_report = base64.b64encode(VALID_REPORT.encode("utf-8")).decode()
            executor = [
                sys.executable,
                "-c",
                (
                    "import base64,sys; sys.stdin.buffer.read(); "
                    f"sys.stdout.buffer.write(base64.b64decode('{encoded_report}'))"
                ),
                secret,
            ]
            args = self._args(
                root=root,
                executor=executor,
                protocol="critic-contrastivist",
            )

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(critic_runner.run(args), 0)
            run_dir = next((root / "runs").iterdir())
            report_bytes = (run_dir / "report.md").read_bytes()
            manifest_text = (run_dir / "manifest.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)

            self.assertEqual(report_bytes, VALID_REPORT.encode("utf-8"))
            self.assertNotIn(secret, manifest_text)
            self.assertNotIn(str(root), manifest_text)
            self.assertEqual(manifest["executor"]["command"], Path(sys.executable).name)
            self.assertEqual(manifest["executor"]["argument_count"], 3)
            self.assertEqual(manifest["status"], "succeeded")
            self.assertEqual(manifest["executor_returncode"], 0)
            self.assertEqual(manifest["runner_exit_code"], 0)
            self.assertTrue(manifest["report_validation"]["valid"])
            self.assertIsNotNone(manifest["completed_at"])
            self.assertEqual(manifest["timeout_seconds"], 900.0)
            self.assertEqual(
                manifest["report_sha256"], hashlib.sha256(report_bytes).hexdigest()
            )
            verification = critic_runner.verify_run_dir(
                run_dir, Path(args.manuscript)
            )
            self.assertTrue(verification.valid, verification.errors)

    def test_invalid_report_changes_successful_executor_to_exit_three(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executor = [sys.executable, "-c", "print('not a valid report')"]
            args = self._args(root=root, executor=executor)

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.run(args), critic_runner.EXIT_INVALID_REPORT
                )
            manifest = json.loads(
                (next((root / "runs").iterdir()) / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "invalid_report")
            self.assertEqual(manifest["executor_returncode"], 0)
            self.assertEqual(
                manifest["runner_exit_code"], critic_runner.EXIT_INVALID_REPORT
            )
            self.assertFalse(manifest["report_validation"]["valid"])

    def test_nonzero_executor_code_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executor = [sys.executable, "-c", "raise SystemExit(7)"]
            args = self._args(root=root, executor=executor)

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(critic_runner.run(args), 7)
            manifest = json.loads(
                (next((root / "runs").iterdir()) / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["executor_returncode"], 7)
            self.assertEqual(manifest["runner_exit_code"], 7)

    def test_executor_start_failure_is_archived(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root, executor=[str(root / "missing-executor")])

            with redirect_stderr(io.StringIO()):
                self.assertEqual(critic_runner.run(args), 2)
            run_dir = next((root / "runs").iterdir())
            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertTrue((run_dir / "prompt.md").is_file())
            self.assertTrue((run_dir / "stderr.log").is_file())
            self.assertFalse((run_dir / "report.md").exists())
            self.assertEqual(manifest["status"], "start_failed")
            self.assertEqual(manifest["runner_exit_code"], 2)

    def test_interrupted_executor_records_interrupted_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root, executor=["executor"])

            with patch.object(
                critic_runner, "execute_with_limits", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    critic_runner.run(args)

            run_dir = next((root / "runs").iterdir())
            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "interrupted")
            self.assertEqual(
                manifest["runner_exit_code"], critic_runner.EXIT_INTERRUPTED
            )
            self.assertIsNotNone(manifest["completed_at"])

    def test_timeout_archives_partial_output_and_returns_124(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root, executor=["executor"], timeout=0.25)
            timeout = critic_runner.ExecutorResult(
                returncode=-9,
                stdout=b"partial",
                stderr=b"warning",
                timed_out=True,
            )

            with patch.object(critic_runner, "execute_with_limits", return_value=timeout):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        critic_runner.run(args), critic_runner.EXIT_TIMEOUT
                    )

            run_dir = next((root / "runs").iterdir())
            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual((run_dir / "report.md").read_text(), "partial")
            self.assertIn("timed out after 0.25 seconds", (run_dir / "stderr.log").read_text())
            self.assertEqual(manifest["status"], "timed_out")
            self.assertEqual(manifest["timeout_seconds"], 0.25)
            self.assertEqual(manifest["runner_exit_code"], critic_runner.EXIT_TIMEOUT)

    def test_validate_command_reports_valid_and_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "report.md"
            report.write_text(VALID_REPORT, encoding="utf-8", newline="\n")
            args = argparse.Namespace(
                protocol="critic-individualist", report=str(report)
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.validate_command(args), 0)

            report.write_text("invalid", encoding="utf-8", newline="\n")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.validate_command(args),
                    critic_runner.EXIT_INVALID_REPORT,
                )

    def test_verify_run_command_returns_four_for_corrupt_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
            args = argparse.Namespace(run_dir=str(run_dir), source=None)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.verify_run_command(args),
                    critic_runner.EXIT_INVALID_ARCHIVE,
                )

    def test_timeout_parser_requires_positive_finite_number(self) -> None:
        self.assertEqual(critic_runner.positive_seconds("1.5"), 1.5)
        for value in ("0", "-1", "nan", "inf", "not-a-number"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    critic_runner.positive_seconds(value)

    def test_run_rejects_invalid_timeout_when_called_as_a_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            args = self._args(
                root=Path(temp_dir), executor=["executor"], timeout="invalid"
            )
            with self.assertRaisesRegex(ValueError, "positive finite"):
                critic_runner.run(args)

    def test_run_parser_has_a_bounded_default_timeout(self) -> None:
        args = critic_runner.parser().parse_args(
            ["run", "critic-individualist", "draft.md"]
        )
        self.assertEqual(args.timeout, 900.0)
        self.assertEqual(
            args.max_output_bytes, critic_runner.DEFAULT_MAX_OUTPUT_BYTES
        )

    def test_executor_output_limit_bounds_archived_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(
                root=root,
                executor=[
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * 100000)",
                ],
            )
            args.max_output_bytes = 1024
            with redirect_stderr(io.StringIO()):
                self.assertEqual(critic_runner.run(args), critic_runner.EXIT_OUTPUT_LIMIT)
            run_dir = next((root / "runs").iterdir())
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "output_limit_exceeded")
            self.assertEqual(manifest["runner_exit_code"], critic_runner.EXIT_OUTPUT_LIMIT)
            self.assertLessEqual((run_dir / "report.md").stat().st_size, 1024)
            self.assertTrue(manifest["stdout_truncated"])
            self.assertTrue(
                critic_runner.verify_run_dir(run_dir, Path(args.manuscript)).valid
            )

    def test_invalid_utf8_executor_output_is_archived_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(
                root=root,
                executor=[
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'\\xff')",
                ],
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(critic_runner.run(args), critic_runner.EXIT_INVALID_REPORT)
            run_dir = next((root / "runs").iterdir())
            self.assertEqual((run_dir / "report.md").read_bytes(), b"\xff")
            verification = critic_runner.verify_run_dir(run_dir, Path(args.manuscript))
            self.assertTrue(verification.valid, verification.errors)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits only")
    def test_run_archives_are_private_on_posix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = argparse.Namespace(
                protocol="critic-individualist",
                manuscript=str(root / "draft.md"),
                runs_dir=str(root / "runs"),
                allow_test_artifact=False,
            )
            (root / "draft.md").write_text("draft", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.prepare(args), 0)
            run_dir = next((root / "runs").iterdir())
            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
            for artifact in run_dir.iterdir():
                self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)

    def test_score_divergence_handles_exact_and_ambiguous_pairings(self) -> None:
        scorecard = critic_runner.scorecard_template()
        comparisons = scorecard["comparisons"]
        for name in critic_runner.WITHIN_COMPARISONS:
            comparisons[name] = {
                "overlap": 10,
                "different_reason": 0,
                "left_unique": 0,
                "right_unique": 0,
                "ambiguous": 0,
            }
        for name in critic_runner.BETWEEN_COMPARISONS:
            comparisons[name] = {
                "overlap": 1,
                "different_reason": 8,
                "left_unique": 1,
                "right_unique": 1,
                "ambiguous": 0,
            }
        result = critic_runner.score_divergence(scorecard)
        self.assertEqual(result["verdict"], "advance")
        self.assertEqual(result["W"], {"lower": 0.0, "upper": 0.0})
        self.assertEqual(result["B"]["lower"], result["B"]["upper"])
        self.assertGreater(result["B"]["lower"], 0.9)

        comparisons["I1:C1"]["different_reason"] = 0
        comparisons["I1:C1"]["ambiguous"] = 8
        uncertain = critic_runner.score_divergence(scorecard)
        self.assertLess(
            uncertain["comparisons"]["I1:C1"]["d_lower"],
            uncertain["comparisons"]["I1:C1"]["d_upper"],
        )

    def test_score_divergence_rejects_unfilled_template(self) -> None:
        with self.assertRaisesRegex(critic_runner.ScorecardError, "non-negative integer"):
            critic_runner.score_divergence(critic_runner.scorecard_template())

    def test_score_divergence_covers_reject_inconclusive_empty_and_markdown(self) -> None:
        scorecard = critic_runner.scorecard_template()
        comparisons = scorecard["comparisons"]
        for name in critic_runner.ALL_COMPARISONS:
            comparisons[name] = {
                "overlap": 0,
                "different_reason": 0,
                "left_unique": 0,
                "right_unique": 0,
                "ambiguous": 0,
            }
        empty = critic_runner.score_divergence(scorecard)
        self.assertEqual(empty["verdict"], "reject")
        self.assertEqual(empty["B"], {"lower": 0.0, "upper": 0.0})

        for name in critic_runner.WITHIN_COMPARISONS:
            comparisons[name]["overlap"] = 10
        for name in critic_runner.BETWEEN_COMPARISONS:
            comparisons[name]["overlap"] = 9
            comparisons[name]["different_reason"] = 1
        inconclusive = critic_runner.score_divergence(scorecard)
        self.assertEqual(inconclusive["verdict"], "inconclusive")
        markdown = critic_runner.score_markdown(inconclusive)
        self.assertIn("# Divergence score", markdown)
        self.assertIn("**inconclusive**", markdown)

        for name in critic_runner.WITHIN_COMPARISONS:
            comparisons[name]["overlap"] = 0
            comparisons[name]["different_reason"] = 10
        rejected = critic_runner.score_divergence(scorecard)
        self.assertEqual(rejected["verdict"], "reject")

    def test_score_divergence_rejects_invalid_shapes(self) -> None:
        invalid = (
            None,
            {},
            {"schema_version": 1, "margin": float("nan"), "comparisons": {}},
            {"schema_version": 1, "margin": 0.2, "comparisons": []},
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(critic_runner.ScorecardError):
                    critic_runner.score_divergence(value)

        scorecard = critic_runner.scorecard_template()
        scorecard["comparisons"]["extra"] = {}
        with self.assertRaisesRegex(critic_runner.ScorecardError, "mismatch"):
            critic_runner.score_divergence(scorecard)

        inconsistent = critic_runner.scorecard_template()
        for name in critic_runner.ALL_COMPARISONS:
            inconsistent["comparisons"][name] = {
                "overlap": 1,
                "different_reason": 0,
                "left_unique": 0,
                "right_unique": 0,
                "ambiguous": 0,
            }
        inconsistent["comparisons"]["I1:C1"]["left_unique"] = 1
        with self.assertRaisesRegex(critic_runner.ScorecardError, "inconsistent"):
            critic_runner.score_divergence(inconsistent)

    def test_score_commands_write_template_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scorecard_path = root / "scorecard.json"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    critic_runner.init_scorecard_command(
                        argparse.Namespace(output=str(scorecard_path))
                    ),
                    0,
                )
            scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))
            for name in critic_runner.ALL_COMPARISONS:
                scorecard["comparisons"][name] = {
                    "overlap": 1,
                    "different_reason": 0,
                    "left_unique": 0,
                    "right_unique": 0,
                    "ambiguous": 0,
                }
            scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
            output_path = root / "score.md"
            args = argparse.Namespace(
                scorecard=str(scorecard_path),
                format="markdown",
                output=str(output_path),
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.score_command(args), 0)
            self.assertIn("Divergence score", output_path.read_text(encoding="utf-8"))

            scorecard_path.write_text("{}", encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.score_command(args),
                    critic_runner.EXIT_INVALID_SCORECARD,
                )

    def test_execute_with_limits_times_out_and_rejects_invalid_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = critic_runner.execute_with_limits(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                b"prompt",
                timeout_seconds=0.05,
                max_output_bytes=1024,
                capture_dir=root,
            )
            self.assertTrue(result.timed_out)
            with self.assertRaisesRegex(ValueError, "positive integer"):
                critic_runner.execute_with_limits(
                    [sys.executable, "-c", "pass"],
                    b"prompt",
                    timeout_seconds=1,
                    max_output_bytes=0,
                    capture_dir=root,
                )

    def test_campaign_creates_isolated_runs_summary_and_scorecard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "draft.md"
            source.write_text("draft", encoding="utf-8")
            encoded_report = base64.b64encode(VALID_REPORT.encode("utf-8")).decode()
            args = argparse.Namespace(
                manuscript=str(source),
                protocol=None,
                repeat=2,
                campaigns_dir=str(root / "campaigns"),
                allow_test_artifact=False,
                timeout=5.0,
                max_output_bytes=1024 * 1024,
                executor=[
                    sys.executable,
                    "-c",
                    (
                        "import base64,sys; sys.stdin.buffer.read(); "
                        f"sys.stdout.buffer.write(base64.b64decode('{encoded_report}'))"
                    ),
                ],
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(critic_runner.campaign(args), 0)
            campaign_dir = next((root / "campaigns").iterdir())
            manifest = json.loads(
                (campaign_dir / "campaign.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(manifest["runs"]), 4)
            self.assertEqual(
                [run["label"] for run in manifest["runs"]],
                ["I1", "I2", "C1", "C2"],
            )
            self.assertTrue((campaign_dir / "scorecard.json").is_file())
            self.assertTrue((campaign_dir / "SUMMARY.md").is_file())
            self.assertEqual(len(list((campaign_dir / "runs").iterdir())), 4)
            verification = critic_runner.verify_campaign_dir(campaign_dir, source)
            self.assertTrue(verification.valid, verification.errors)

            scorecard_path = campaign_dir / "scorecard.json"
            scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))
            scorecard["margin"] = 0.3
            scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
            filled = critic_runner.verify_campaign_dir(campaign_dir, source)
            self.assertTrue(filled.valid, filled.errors)
            self.assertTrue(any("filled" in warning for warning in filled.warnings))

            first_run = campaign_dir / manifest["runs"][0]["run_dir"]
            (first_run / "report.md").write_bytes(b"corrupt")
            corrupted = critic_runner.verify_campaign_dir(campaign_dir, source)
            self.assertFalse(corrupted.valid)
            self.assertTrue(any("hash mismatch" in error for error in corrupted.errors))

    def test_cli_run_path_handles_separator_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "draft.md"
            source.write_text("draft", encoding="utf-8")
            executor_script = root / "executor.py"
            encoded_report = base64.b64encode(VALID_REPORT.encode("utf-8")).decode()
            executor_script.write_text(
                "import base64, sys\n"
                "sys.stdin.buffer.read()\n"
                f"sys.stdout.buffer.write(base64.b64decode('{encoded_report}'))\n",
                encoding="utf-8",
                newline="\n",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "critic_runner.py"),
                    "run",
                    "critic-individualist",
                    str(source),
                    "--runs-dir",
                    str(root / "runs"),
                    "--timeout",
                    "5",
                    "--",
                    sys.executable,
                    str(executor_script),
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report_path = Path(completed.stdout.strip())
            self.assertEqual(report_path.read_text(encoding="utf-8"), VALID_REPORT)

    def test_cli_campaign_score_and_verify_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "draft.md"
            source.write_text("draft", encoding="utf-8")
            executor_script = root / "executor.py"
            encoded_report = base64.b64encode(VALID_REPORT.encode("utf-8")).decode()
            executor_script.write_text(
                "import base64, sys\n"
                "sys.stdin.buffer.read()\n"
                f"sys.stdout.buffer.write(base64.b64decode('{encoded_report}'))\n",
                encoding="utf-8",
                newline="\n",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "critic_runner.py"),
                    "campaign",
                    str(source),
                    "--campaigns-dir",
                    str(root / "campaigns"),
                    "--timeout",
                    "5",
                    "--",
                    sys.executable,
                    str(executor_script),
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            campaign_dir = Path(completed.stdout.strip())

            verified = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "critic_runner.py"),
                    "verify-campaign",
                    str(campaign_dir),
                    "--source",
                    str(source),
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)

            scorecard_path = campaign_dir / "scorecard.json"
            scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))
            for name in critic_runner.ALL_COMPARISONS:
                scorecard["comparisons"][name] = {
                    "overlap": 1,
                    "different_reason": 0,
                    "left_unique": 0,
                    "right_unique": 0,
                    "ambiguous": 0,
                }
            scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
            scored = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "critic_runner.py"),
                    "score",
                    str(scorecard_path),
                    "--format",
                    "markdown",
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            self.assertEqual(scored.returncode, 0, scored.stderr)
            self.assertIn("Verdict: **reject**", scored.stdout)


if __name__ == "__main__":
    unittest.main()
