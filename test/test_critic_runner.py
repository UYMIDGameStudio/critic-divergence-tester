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
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import critic_runner  # noqa: E402
import critic_execution  # noqa: E402
import critic_workflow  # noqa: E402


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

    def test_json_parser_rejects_duplicate_keys(self) -> None:
        with self.assertRaisesRegex(critic_runner.DuplicateJsonKeyError, "duplicate"):
            critic_runner.parse_json('{"status": "succeeded", "status": "failed"}')

    def test_partial_adjudication_distinguishes_undecided_from_invalid(self) -> None:
        value = critic_workflow.adjudication_template(
            protocol="critic-social-science",
            report_sha256="a" * 64,
            manifest_sha256="b" * 64,
            findings=[
                {
                    "id": "A1",
                    "position": "原文",
                    "claim": "缺少一步",
                    "reason": "理由",
                    "test": "补足",
                    "conclusion": "缩窄",
                }
            ],
        )
        self.assertEqual(
            critic_workflow.validate_adjudication(value, require_complete=False), []
        )
        value["findings"][0]["decision"] = "accept"
        errors = critic_workflow.validate_adjudication(
            value, require_complete=False
        )
        self.assertTrue(any("revision_action is required" in error for error in errors))

    def test_generic_protocol_requires_explicit_unlock(self) -> None:
        with self.assertRaisesRegex(ValueError, "test artifact"):
            critic_runner.load_protocol("critic-generic")

        body, raw = critic_runner.load_protocol(
            "critic-generic", allow_test_artifact=True
        )
        self.assertIn("你审查一篇文章的论证", body)
        self.assertTrue(raw.startswith(b"---"))

    def test_academic_tracks_are_first_class_and_resolve_to_real_protocols(self) -> None:
        self.assertEqual(
            set(critic_runner.ACADEMIC_TRACKS),
            {"humanities-social-science", "natural-science", "engineering"},
        )
        primaries = []
        for track in critic_runner.ACADEMIC_TRACKS.values():
            primary = track["primary"]
            primaries.append(primary)
            self.assertIn(primary, critic_runner.CRITIC_PROTOCOLS)
            self.assertNotIn(primary, critic_runner.TEST_ONLY)
            for specialist in track["specialists"]:
                self.assertIn(specialist, critic_runner.CRITIC_PROTOCOLS)
        self.assertEqual(len(primaries), len(set(primaries)))
        self.assertEqual(
            critic_runner.CROSS_DISCIPLINARY_PROTOCOLS,
            ("citation-auditor",),
        )

    def test_track_prompts_encode_distinct_method_contracts(self) -> None:
        social, _ = critic_runner.load_protocol("critic-social-science")
        natural, _ = critic_runner.load_protocol("critic-natural-science")
        engineering, _ = critic_runner.load_protocol("critic-engineering")
        self.assertIn("先分型，后审查", social)
        self.assertIn("不得机械要求 p 值", social)
        self.assertIn("不得要求它虚构因果识别策略", social)
        self.assertIn("仪器校准", natural)
        self.assertIn("误差传播", natural)
        self.assertIn("验证是否证明", engineering)
        self.assertIn("确认是否证明", engineering)

    def test_tracks_command_exposes_primary_and_specialist_protocols(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(critic_runner.list_tracks(argparse.Namespace()), 0)
        rendered = output.getvalue()
        self.assertIn("humanities-social-science: 文科·社会科学", rendered)
        self.assertIn("primary: critic-social-science", rendered)
        self.assertIn("critic-individualist, critic-contrastivist", rendered)
        self.assertIn("cross-disciplinary: citation-auditor", rendered)

    def test_doctor_checks_a_fresh_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            errors = io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                result = critic_runner.doctor(
                    argparse.Namespace(directory=temp_dir)
                )
            self.assertEqual(result, 0)
            self.assertIn("academic tracks resolve correctly", output.getvalue())
            self.assertTrue(output.getvalue().rstrip().endswith("ready"))
            self.assertEqual(errors.getvalue(), "")

    def test_doctor_reports_an_unreadable_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.md"
            protocols = dict(critic_runner.PROTOCOLS)
            protocols["critic-social-science"] = missing
            output = io.StringIO()
            errors = io.StringIO()
            with patch.object(critic_runner, "PROTOCOLS", protocols):
                with redirect_stdout(output), redirect_stderr(errors):
                    result = critic_runner.doctor(
                        argparse.Namespace(directory=temp_dir)
                    )
            self.assertEqual(result, 2)
            self.assertNotIn("\nready\n", f"\n{output.getvalue()}")
            self.assertIn("critic-social-science cannot be loaded", errors.getvalue())

    def test_quickstart_guides_track_selection_and_accepts_a_quoted_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "my draft.md"
            source.write_text("工程论证", encoding="utf-8")
            output = io.StringIO()
            errors = io.StringIO()
            with patch(
                "builtins.input",
                side_effect=[f'"{source}"', "不是选项", "3"],
            ):
                with redirect_stdout(output), redirect_stderr(errors):
                    result = critic_runner.quickstart(
                        argparse.Namespace(
                            manuscript=None,
                            track=None,
                            runs_dir=str(root / "runs"),
                        )
                    )
            self.assertEqual(result, 0)
            self.assertEqual(errors.getvalue(), "")
            self.assertIn("无法识别", output.getvalue())
            self.assertIn("已选择：工科·工程学", output.getvalue())
            self.assertIn("import-report", output.getvalue())
            self.assertIn("adjudicate", output.getvalue())
            run_dir = next((root / "runs").iterdir())
            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["protocol"], "critic-engineering")

    def test_quickstart_defaults_to_humanities_on_an_empty_track_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "draft.md"
            source.write_text("社会科学论证", encoding="utf-8")
            output = io.StringIO()
            with patch("builtins.input", return_value="") as prompt:
                with redirect_stdout(output), redirect_stderr(io.StringIO()):
                    result = critic_runner.quickstart(
                        argparse.Namespace(
                            manuscript=str(source),
                            track=None,
                            runs_dir=str(root / "runs"),
                        )
                    )
            self.assertEqual(result, 0)
            prompt.assert_called_once()
            self.assertIn("已选择：文科·社会科学", output.getvalue())

    def test_quickstart_rejects_missing_or_empty_input_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty = root / "empty.md"
            empty.write_text(" \n", encoding="utf-8")
            invalid_utf8 = root / "invalid.md"
            invalid_utf8.write_bytes(b"\xff")
            for response in (
                "",
                str(root / "missing.md"),
                str(empty),
                str(invalid_utf8),
            ):
                errors = io.StringIO()
                with patch("builtins.input", return_value=response):
                    with redirect_stdout(io.StringIO()), redirect_stderr(errors):
                        result = critic_runner.quickstart(
                            argparse.Namespace(
                                manuscript=None,
                                track=None,
                                runs_dir=str(root / "runs"),
                            )
                        )
                self.assertEqual(result, 2)
                self.assertIn("错误：", errors.getvalue())

    def test_quickstart_cli_supports_an_explicit_track(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "draft.md"
            source.write_text("自然科学论证", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "critic_runner.py"),
                    "quickstart",
                    str(source),
                    "--track",
                    "natural-science",
                    "--runs-dir",
                    str(root / "runs"),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("已选择：理科·自然科学", completed.stdout)
            manifest = json.loads(
                next((root / "runs").glob("*/manifest.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["protocol"], "critic-natural-science")

    def test_quickstart_rejects_an_unknown_programmatic_track(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "draft.md"
            source.write_text("论证", encoding="utf-8")
            errors = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(errors):
                result = critic_runner.quickstart(
                    argparse.Namespace(
                        manuscript=str(source),
                        track="unknown",
                        runs_dir=str(Path(temp_dir) / "runs"),
                    )
                )
            self.assertEqual(result, 2)
            self.assertIn("未知学术线", errors.getvalue())

    def test_prepare_track_archives_the_track_primary_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "draft.md"
            source.write_text("draft", encoding="utf-8")
            args = argparse.Namespace(
                track="humanities-social-science",
                manuscript=str(source),
                runs_dir=str(root / "runs"),
                allow_test_artifact=False,
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.prepare_track(args), 0)
            run_dir = next((root / "runs").iterdir())
            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["protocol"], "critic-social-science")
            self.assertIn(
                "第一原则：先分型，后审查",
                (run_dir / "prompt.md").read_text(encoding="utf-8"),
            )

    def test_import_report_closes_manual_run_and_creates_adjudication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(critic_runner.prepare(args), 0)
            run_dir = next((root / "runs").iterdir())
            returned_report = root / "ai-response.md"
            returned_report.write_text(VALID_REPORT, encoding="utf-8")

            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                result = critic_runner.import_report_command(
                    argparse.Namespace(
                        run_dir=str(run_dir),
                        report=str(returned_report),
                        adjudication_output=None,
                    )
                )
            self.assertEqual(result, 0)
            self.assertIn("adjudication.json", output.getvalue())
            self.assertEqual((run_dir / "report.md").read_text(encoding="utf-8"), VALID_REPORT)
            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            manifest_text = (run_dir / "manifest.json").read_text(encoding="utf-8")
            self.assertEqual(manifest["schema_version"], 3)
            self.assertEqual(manifest["status"], "collected")
            self.assertIsNone(manifest["executor"])
            self.assertEqual(manifest["collection"]["method"], "manual-import")
            self.assertEqual(manifest["collection"]["source_name"], "ai-response.md")
            self.assertNotIn(str(root), manifest_text)
            adjudication = json.loads(
                (run_dir / "adjudication.json").read_text(encoding="utf-8")
            )
            self.assertEqual(adjudication["findings"][0]["claim"], "缺少关键一步。")
            self.assertEqual(adjudication["findings"][0]["test"], "补足后的论证。")
            self.assertEqual(adjudication["source"]["report_status"], "complete")
            self.assertEqual(adjudication["source"]["unverified"], "none")
            self.assertIsNone(adjudication["findings"][0]["decision"])
            verification = critic_runner.verify_run_dir(run_dir)
            self.assertTrue(verification.valid, verification.errors)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.import_report_command(
                        argparse.Namespace(
                            run_dir=str(run_dir),
                            report=str(returned_report),
                            adjudication_output=None,
                        )
                    ),
                    critic_runner.EXIT_INVALID_WORKFLOW,
                )
            manifest["collection"]["imported_at"] = "2026-01-01T00:00:00+00:00"
            (run_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            tampered = critic_runner.verify_run_dir(run_dir)
            self.assertFalse(tampered.valid)
            self.assertTrue(
                any("imported_at must equal" in error for error in tampered.errors)
            )

    def test_import_report_preserves_partial_status_and_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root)
            with redirect_stdout(io.StringIO()):
                critic_runner.prepare(args)
            run_dir = next((root / "runs").iterdir())
            partial_report = VALID_REPORT.replace(
                "STATUS: complete\nUNVERIFIED: none",
                "STATUS: partial\nUNVERIFIED: 原始数据尚未提供",
            )
            report = root / "partial.md"
            report.write_text(partial_report, encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.import_report_command(
                        argparse.Namespace(
                            run_dir=str(run_dir),
                            report=str(report),
                            adjudication_output=None,
                        )
                    ),
                    0,
                )
            adjudication = json.loads(
                (run_dir / "adjudication.json").read_text(encoding="utf-8")
            )
            self.assertEqual(adjudication["source"]["report_status"], "partial")
            self.assertEqual(
                adjudication["source"]["unverified"], "原始数据尚未提供"
            )

    def test_revision_plan_requires_human_decisions_and_preserves_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root)
            with redirect_stdout(io.StringIO()):
                critic_runner.prepare(args)
            run_dir = next((root / "runs").iterdir())
            returned_report = root / "report.md"
            returned_report.write_text(VALID_REPORT, encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.import_report_command(
                        argparse.Namespace(
                            run_dir=str(run_dir),
                            report=str(returned_report),
                            adjudication_output=None,
                        )
                    ),
                    0,
                )

            plan_args = argparse.Namespace(
                run_dir=str(run_dir), adjudication=None, output=None
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.revision_plan_command(plan_args),
                    critic_runner.EXIT_INVALID_WORKFLOW,
                )
            adjudication_path = run_dir / "adjudication.json"
            adjudication = json.loads(adjudication_path.read_text(encoding="utf-8"))
            adjudication["findings"][0]["decision"] = "accept"
            adjudication["findings"][0]["author_reason"] = "批评成立。"
            adjudication["findings"][0]["revision_action"] = "补写关键推导并增加反例。"
            adjudication_path.write_text(
                json.dumps(adjudication, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(critic_runner.revision_plan_command(plan_args), 0)
            plan = (run_dir / "revision-plan.md").read_text(encoding="utf-8")
            self.assertIn("接受：1", plan)
            self.assertIn("裁决 SHA-256", plan)
            self.assertIn("报告状态：`complete`", plan)
            self.assertIn("补写关键推导并增加反例。", plan)
            self.assertIn(manifest_hash := hashlib.sha256(
                (run_dir / "manifest.json").read_bytes()
            ).hexdigest(), adjudication_path.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest_hash), 64)

    def test_import_report_rejects_invalid_input_without_mutating_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root)
            with redirect_stdout(io.StringIO()):
                critic_runner.prepare(args)
            run_dir = next((root / "runs").iterdir())
            original_manifest = (run_dir / "manifest.json").read_bytes()
            inside_report = run_dir / "returned.md"
            inside_report.write_text(VALID_REPORT, encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.import_report_command(
                        argparse.Namespace(
                            run_dir=str(run_dir),
                            report=str(inside_report),
                            adjudication_output=None,
                        )
                    ),
                    critic_runner.EXIT_INVALID_WORKFLOW,
                )
            invalid_report = root / "invalid.md"
            invalid_report.write_text("not a valid report", encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = critic_runner.import_report_command(
                    argparse.Namespace(
                        run_dir=str(run_dir),
                        report=str(invalid_report),
                        adjudication_output=None,
                    )
                )
            self.assertEqual(result, critic_runner.EXIT_INVALID_REPORT)
            self.assertEqual((run_dir / "manifest.json").read_bytes(), original_manifest)
            self.assertFalse((run_dir / "report.md").exists())
            self.assertFalse((run_dir / "adjudication.json").exists())

    def test_revision_plan_rejects_edited_critic_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root)
            with redirect_stdout(io.StringIO()):
                critic_runner.prepare(args)
            run_dir = next((root / "runs").iterdir())
            report = root / "report.md"
            report.write_text(VALID_REPORT, encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                critic_runner.import_report_command(
                    argparse.Namespace(
                        run_dir=str(run_dir),
                        report=str(report),
                        adjudication_output=None,
                    )
                )
            adjudication_path = run_dir / "adjudication.json"
            adjudication = json.loads(adjudication_path.read_text(encoding="utf-8"))
            finding = adjudication["findings"][0]
            finding["claim"] = "被篡改的批评"
            finding["decision"] = "reject"
            finding["author_reason"] = "不成立。"
            adjudication_path.write_text(
                json.dumps(adjudication, ensure_ascii=False), encoding="utf-8"
            )
            errors = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(errors):
                result = critic_runner.revision_plan_command(
                    argparse.Namespace(
                        run_dir=str(run_dir), adjudication=None, output=None
                    )
                )
            self.assertEqual(result, critic_runner.EXIT_INVALID_ARCHIVE)
            self.assertIn("edited outside decision fields", errors.getvalue())
            self.assertFalse((run_dir / "revision-plan.md").exists())

    def test_adjudicate_guides_human_decisions_and_autogenerates_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root)
            with redirect_stdout(io.StringIO()):
                critic_runner.prepare(args)
            run_dir = next((root / "runs").iterdir())
            report = root / "report.md"
            report.write_text(VALID_REPORT, encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                critic_runner.import_report_command(
                    argparse.Namespace(
                        run_dir=str(run_dir),
                        report=str(report),
                        adjudication_output=None,
                    )
                )

            output = io.StringIO()
            with patch(
                "builtins.input",
                side_effect=["未知", "1", "批评成立", "", "补写关键推导"],
            ):
                with redirect_stdout(output), redirect_stderr(io.StringIO()):
                    result = critic_runner.adjudicate_command(
                        argparse.Namespace(run_dir=str(run_dir))
                    )
            self.assertEqual(result, 0)
            self.assertIn("无法识别", output.getvalue())
            self.assertIn("必须写清", output.getvalue())
            adjudication = json.loads(
                (run_dir / "adjudication.json").read_text(encoding="utf-8")
            )
            self.assertEqual(adjudication["findings"][0]["decision"], "accept")
            self.assertEqual(
                adjudication["findings"][0]["revision_action"], "补写关键推导"
            )
            self.assertIn(
                "补写关键推导",
                (run_dir / "revision-plan.md").read_text(encoding="utf-8"),
            )
            with patch("builtins.input") as prompt:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        critic_runner.adjudicate_command(
                            argparse.Namespace(run_dir=str(run_dir))
                        ),
                        0,
                    )
            prompt.assert_not_called()

            plan_path = run_dir / "revision-plan.md"
            original_plan = plan_path.read_text(encoding="utf-8")
            plan_path.write_text(original_plan + "\n被手工修改", encoding="utf-8")
            errors = io.StringIO()
            with patch("builtins.input") as prompt:
                with redirect_stdout(io.StringIO()), redirect_stderr(errors):
                    self.assertEqual(
                        critic_runner.adjudicate_command(
                            argparse.Namespace(run_dir=str(run_dir))
                        ),
                        critic_runner.EXIT_INVALID_ARCHIVE,
                    )
            prompt.assert_not_called()
            self.assertIn("revision-plan.md does not match", errors.getvalue())
            plan_path.write_text(original_plan, encoding="utf-8")

            adjudication["findings"][0]["author_reason"] = "修改后的采纳理由"
            (run_dir / "adjudication.json").write_text(
                json.dumps(adjudication, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            errors = io.StringIO()
            with patch("builtins.input") as prompt:
                with redirect_stdout(io.StringIO()), redirect_stderr(errors):
                    self.assertEqual(
                        critic_runner.adjudicate_command(
                            argparse.Namespace(run_dir=str(run_dir))
                        ),
                        critic_runner.EXIT_INVALID_ARCHIVE,
                    )
            prompt.assert_not_called()
            self.assertIn("revision-plan.md does not match", errors.getvalue())

    def test_import_report_supports_citation_runs_without_fake_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root, protocol="citation-auditor")
            with redirect_stdout(io.StringIO()):
                critic_runner.prepare(args)
            run_dir = next((root / "runs").iterdir())
            report = root / "citation-report.md"
            report.write_text(VALID_CITATION_REPORT, encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = critic_runner.import_report_command(
                    argparse.Namespace(
                        run_dir=str(run_dir),
                        report=str(report),
                        adjudication_output=None,
                    )
                )
            self.assertEqual(result, 0)
            self.assertFalse((run_dir / "adjudication.json").exists())
            self.assertTrue(critic_runner.verify_run_dir(run_dir).valid)

    def test_empty_valid_report_needs_no_fake_human_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root)
            with redirect_stdout(io.StringIO()):
                critic_runner.prepare(args)
            run_dir = next((root / "runs").iterdir())
            report = root / "empty-report.md"
            report.write_text(EMPTY_VALID_REPORT, encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.import_report_command(
                        argparse.Namespace(
                            run_dir=str(run_dir),
                            report=str(report),
                            adjudication_output=None,
                        )
                    ),
                    0,
                )
            adjudication = json.loads(
                (run_dir / "adjudication.json").read_text(encoding="utf-8")
            )
            self.assertEqual(adjudication["findings"], [])
            with patch("builtins.input") as prompt:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        critic_runner.adjudicate_command(
                            argparse.Namespace(run_dir=str(run_dir))
                        ),
                        0,
                    )
            prompt.assert_not_called()
            self.assertIn(
                "没有需要裁决的发现",
                (run_dir / "revision-plan.md").read_text(encoding="utf-8"),
            )

    def test_manual_workflow_cli_runs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(root=root, protocol="critic-social-science")
            with redirect_stdout(io.StringIO()):
                critic_runner.prepare(args)
            run_dir = next((root / "runs").iterdir())
            report = root / "returned.md"
            report.write_text(VALID_REPORT, encoding="utf-8")
            imported = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "critic_runner.py"),
                    "import-report",
                    str(run_dir),
                    str(report),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            adjudicated = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "critic_runner.py"),
                    "adjudicate",
                    str(run_dir),
                ],
                cwd=root,
                input="1\n批评成立\n补写推导\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(adjudicated.returncode, 0, adjudicated.stderr)
            self.assertIn("裁决完成", adjudicated.stdout)
            self.assertIn(
                "补写推导",
                (run_dir / "revision-plan.md").read_text(encoding="utf-8"),
            )

    def test_campaign_schedule_is_seeded_and_counterbalanced(self) -> None:
        protocols = [
            "critic-social-science",
            "critic-natural-science",
            "critic-engineering",
        ]
        first = critic_runner.campaign_schedule(protocols, 3, "published-seed")
        second = critic_runner.campaign_schedule(protocols, 3, "published-seed")
        self.assertEqual(first, second)
        round_one = [protocol for protocol, repetition in first if repetition == 1]
        round_two = [protocol for protocol, repetition in first if repetition == 2]
        round_three = [protocol for protocol, repetition in first if repetition == 3]
        self.assertCountEqual(round_one, protocols)
        self.assertEqual(round_two, list(reversed(round_one)))
        self.assertEqual(round_three, round_one)

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
            self.assertEqual(manifest["schema_version"], 3)
            self.assertIsNone(manifest["collection"])
            self.assertEqual(manifest["status"], "prepared")
            self.assertEqual(manifest["started_at"], manifest["completed_at"])
            self.assertEqual(manifest["runner_exit_code"], 0)
            verification = critic_runner.verify_run_dir(run_dir, source)
            self.assertTrue(verification.valid, verification.errors)
            self.assertEqual(verification.warnings, ())
            manifest["schema_version"] = 2
            manifest.pop("collection")
            (run_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            legacy_verification = critic_runner.verify_run_dir(run_dir, source)
            self.assertTrue(legacy_verification.valid, legacy_verification.errors)

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
            args.executor_label = "fixture-model; temperature=0"

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
            self.assertEqual(
                manifest["executor"]["label"], "fixture-model; temperature=0"
            )
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

    def test_executor_label_rejects_control_characters(self) -> None:
        with self.assertRaisesRegex(ValueError, "executor label"):
            critic_runner.normalize_executor_label("model\nsecret")

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
                timeout=2,
                executor=[
                    sys.executable,
                    "-c",
                    (
                        "import sys,time\n"
                        "while True:\n"
                        " sys.stdout.buffer.write(b'x' * 4096)\n"
                        " sys.stdout.buffer.flush()\n"
                        " time.sleep(0.005)\n"
                    ),
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
            report = root / "returned.md"
            report.write_text(VALID_REPORT, encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.import_report_command(
                        argparse.Namespace(
                            run_dir=str(run_dir),
                            report=str(report),
                            adjudication_output=None,
                        )
                    ),
                    0,
                )
            with patch("builtins.input", side_effect=["1", "", "补写推导"]):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        critic_runner.adjudicate_command(
                            argparse.Namespace(run_dir=str(run_dir))
                        ),
                        0,
                    )
            for artifact in run_dir.iterdir():
                self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "symbolic-link archive attack")
    def test_verifiers_reject_symbolic_link_artifacts_and_run_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = self._args(
                root=root,
                executor=[sys.executable, "-c", "print('invalid')"],
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(critic_runner.run(args), critic_runner.EXIT_INVALID_REPORT)
            run_dir = next((root / "runs").iterdir())
            report_path = run_dir / "report.md"
            external = root / "external.md"
            external.write_bytes(report_path.read_bytes())
            report_path.unlink()
            report_path.symlink_to(external)
            verification = critic_runner.verify_run_dir(run_dir, Path(args.manuscript))
            self.assertFalse(verification.valid)
            self.assertTrue(any("symbolic link" in error for error in verification.errors))

            campaign_dir = root / "campaign"
            (campaign_dir / "runs").mkdir(parents=True)
            (campaign_dir / "runs" / "escaped").symlink_to(
                root / "outside", target_is_directory=True
            )
            self.assertIsNone(
                critic_runner._safe_campaign_run_path(
                    campaign_dir, "runs/escaped"
                )
            )

            scorecard_target = root / "scorecard-target.json"
            scorecard_target.write_text("{}", encoding="utf-8")
            scorecard_link = root / "scorecard-link.json"
            scorecard_link.symlink_to(scorecard_target)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.blind_scorecard_command(
                        argparse.Namespace(
                            scorecard=str(scorecard_link),
                            output=str(root / "blind.json"),
                            key_output=str(root / "key.json"),
                            seed="seed",
                        )
                    ),
                    critic_runner.EXIT_INVALID_SCORECARD,
                )

            manual_root = root / "manual"
            manual_root.mkdir()
            manual_args = self._args(root=manual_root)
            with redirect_stdout(io.StringIO()):
                critic_runner.prepare(manual_args)
            manual_run = next((manual_root / "runs").iterdir())
            returned_target = manual_root / "returned-target.md"
            returned_target.write_text(VALID_REPORT, encoding="utf-8")
            returned_link = manual_root / "returned-link.md"
            returned_link.symlink_to(returned_target)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.import_report_command(
                        argparse.Namespace(
                            run_dir=str(manual_run),
                            report=str(returned_link),
                            adjudication_output=None,
                        )
                    ),
                    critic_runner.EXIT_INVALID_WORKFLOW,
                )
                self.assertEqual(
                    critic_runner.import_report_command(
                        argparse.Namespace(
                            run_dir=str(manual_run),
                            report=str(returned_target),
                            adjudication_output=None,
                        )
                    ),
                    0,
                )
            adjudication_path = manual_run / "adjudication.json"
            external_adjudication = manual_root / "external-adjudication.json"
            external_adjudication.write_bytes(adjudication_path.read_bytes())
            adjudication_path.unlink()
            adjudication_path.symlink_to(external_adjudication)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.adjudicate_command(
                        argparse.Namespace(run_dir=str(manual_run))
                    ),
                    critic_runner.EXIT_INVALID_WORKFLOW,
                )

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

    def test_pairing_scorecard_derives_counts_from_traceable_pairs(self) -> None:
        claims = [
            {"id": "A1", "position": "p1", "claim": "c1", "reason": "r1"},
            {"id": "A2", "position": "p2", "claim": "c2", "reason": "r2"},
        ]
        runs = {
            name: {
                "archive": f"runs/{name}",
                "report_sha256": "a" * 64,
                "claims": [dict(claim) for claim in claims],
            }
            for name in ("I1", "I2", "C1", "C2")
        }
        scorecard = critic_runner.pairing_scorecard(runs)
        for name in critic_runner.ALL_COMPARISONS:
            scorecard["comparisons"][name] = {
                "complete": True,
                "pairs": [
                    {
                        "left": "A1",
                        "right": "A1",
                        "classification": "overlap",
                    }
                ],
            }
        result = critic_runner.score_divergence(scorecard)
        self.assertEqual(result["schema_version"], 2)
        comparison = result["comparisons"]["I1:I2"]
        self.assertEqual(comparison["overlap"], 1)
        self.assertEqual(comparison["left_unique"], 1)
        self.assertEqual(comparison["right_unique"], 1)
        self.assertEqual(comparison["denominator"], 3)

    def test_pairing_scorecard_rejects_incomplete_unknown_and_reused_claims(self) -> None:
        claim = {"id": "A1", "position": "p", "claim": "c", "reason": "r"}
        runs = {
            name: {
                "archive": f"runs/{name}",
                "report_sha256": "a" * 64,
                "claims": [dict(claim)],
            }
            for name in ("I1", "I2", "C1", "C2")
        }
        scorecard = critic_runner.pairing_scorecard(runs)
        with self.assertRaisesRegex(critic_runner.ScorecardError, "complete must be true"):
            critic_runner.score_divergence(scorecard)

        for name in critic_runner.ALL_COMPARISONS:
            scorecard["comparisons"][name] = {"complete": True, "pairs": []}
        scorecard["comparisons"]["I1:I2"]["pairs"] = [
            {"left": "A2", "right": "A1", "classification": "overlap"}
        ]
        with self.assertRaisesRegex(critic_runner.ScorecardError, "not a claim"):
            critic_runner.score_divergence(scorecard)

        scorecard["comparisons"]["I1:I2"]["pairs"] = [
            {"left": "A1", "right": "A1", "classification": "overlap"},
            {"left": "A1", "right": "A1", "classification": "ambiguous"},
        ]
        with self.assertRaisesRegex(critic_runner.ScorecardError, "one-to-one"):
            critic_runner.score_divergence(scorecard)

    def test_dynamic_scorecard_scores_three_balanced_academic_tracks(self) -> None:
        claim = {"id": "A1", "position": "p", "claim": "c", "reason": "r"}
        runs = {}
        for prefix, protocol in (
            ("S", "critic-social-science"),
            ("N", "critic-natural-science"),
            ("E", "critic-engineering"),
        ):
            for repetition in (1, 2):
                runs[f"{prefix}{repetition}"] = {
                    "protocol": protocol,
                    "repetition": repetition,
                    "archive": f"runs/{prefix}{repetition}",
                    "report_sha256": "a" * 64,
                    "claims": [dict(claim)],
                }
        scorecard = critic_runner.campaign_pairing_scorecard(runs)
        self.assertEqual(scorecard["schema_version"], 3)
        self.assertEqual(len(scorecard["comparisons"]), 15)

        preview = dict(scorecard)
        preview["comparisons"] = {
            name: {"complete": True, "pairs": []}
            for name in scorecard["comparisons"]
        }
        layout = critic_runner.score_divergence(preview)
        self.assertEqual(len(layout["within_comparisons"]), 3)
        self.assertEqual(len(layout["between_comparisons"]), 12)

        for name in layout["within_comparisons"]:
            scorecard["comparisons"][name] = {
                "complete": True,
                "pairs": [
                    {"left": "A1", "right": "A1", "classification": "overlap"}
                ],
            }
        for name in layout["between_comparisons"]:
            scorecard["comparisons"][name] = {
                "complete": True,
                "pairs": [
                    {
                        "left": "A1",
                        "right": "A1",
                        "classification": "different_reason",
                    }
                ],
            }
        result = critic_runner.score_divergence(scorecard)
        self.assertEqual(result["schema_version"], 3)
        self.assertEqual(result["W"], {"lower": 0.0, "upper": 0.0})
        self.assertEqual(result["B"], {"lower": 1.0, "upper": 1.0})
        self.assertEqual(result["verdict"], "advance")
        self.assertIn("S1:S2", critic_runner.score_markdown(result))

    def test_dynamic_scorecard_rejects_unbalanced_or_ambiguous_run_identity(self) -> None:
        claim = {"id": "A1", "position": "p", "claim": "c", "reason": "r"}

        def run(protocol: str, repetition: int) -> dict[str, object]:
            return {
                "protocol": protocol,
                "repetition": repetition,
                "archive": "runs/example",
                "report_sha256": "a" * 64,
                "claims": [dict(claim)],
            }

        with self.assertRaisesRegex(critic_runner.ScorecardError, "same repeat count"):
            critic_runner.campaign_pairing_scorecard(
                {
                    "S1": run("social", 1),
                    "S2": run("social", 2),
                    "N1": run("natural", 1),
                    "N2": run("natural", 2),
                    "N3": run("natural", 3),
                }
            )
        with self.assertRaisesRegex(critic_runner.ScorecardError, "duplicate"):
            critic_runner.campaign_pairing_scorecard(
                {
                    "S1": run("social", 1),
                    "S2": run("social", 1),
                    "N1": run("natural", 1),
                    "N2": run("natural", 2),
                }
            )
        with self.assertRaisesRegex(critic_runner.ScorecardError, "colon-free"):
            critic_runner.campaign_pairing_scorecard(
                {
                    "S:1": run("social", 1),
                    "S2": run("social", 2),
                    "N1": run("natural", 1),
                    "N2": run("natural", 2),
                }
            )
        with self.assertRaisesRegex(critic_runner.ScorecardError, "continuous"):
            critic_runner.campaign_pairing_scorecard(
                {
                    "S1": run("social", 1),
                    "S3": run("social", 3),
                    "N1": run("natural", 1),
                    "N2": run("natural", 2),
                }
            )

    def test_blind_bundle_hides_identity_and_round_trips_pairings(self) -> None:
        claim = {"id": "A1", "position": "p", "claim": "c", "reason": "r"}
        runs = {}
        for prefix, protocol in (
            ("S", "critic-social-science"),
            ("N", "critic-natural-science"),
        ):
            for repetition in (1, 2):
                runs[f"{prefix}{repetition}"] = {
                    "protocol": protocol,
                    "repetition": repetition,
                    "archive": f"runs/{prefix}{repetition}",
                    "report_sha256": "a" * 64,
                    "claims": [dict(claim)],
                }
        scorecard = critic_runner.campaign_pairing_scorecard(runs)
        blind, key = critic_runner.create_blind_bundle(scorecard, "blind-seed")
        repeated_blind, repeated_key = critic_runner.create_blind_bundle(
            scorecard, "blind-seed"
        )
        self.assertEqual(blind, repeated_blind)
        self.assertEqual(key, repeated_key)
        rendered_blind = json.dumps(blind, ensure_ascii=False)
        self.assertNotIn("critic-social-science", rendered_blind)
        self.assertNotIn("critic-natural-science", rendered_blind)
        self.assertNotIn("report_sha256", rendered_blind)
        self.assertNotIn("archive", rendered_blind)
        self.assertNotIn("source_fingerprint", rendered_blind)
        self.assertNotIn("repetition", rendered_blind)
        self.assertNotIn('"S1"', rendered_blind)
        self.assertNotIn('"N1"', rendered_blind)
        self.assertEqual(set(blind["runs"]), {"R01", "R02", "R03", "R04"})

        for comparison in blind["comparisons"].values():
            comparison["complete"] = True
            comparison["pairs"] = [
                {"left": "A1", "right": "A1", "classification": "overlap"}
            ]
        merged = critic_runner.apply_blind_pairings(scorecard, blind, key)
        result = critic_runner.score_divergence(merged)
        self.assertEqual(result["verdict"], "reject")
        self.assertEqual(set(merged["comparisons"]), set(scorecard["comparisons"]))

    def test_blind_bundle_rejects_modified_evidence_key_and_pair_ids(self) -> None:
        claim = {"id": "A1", "position": "p", "claim": "c", "reason": "r"}
        runs = {
            name: {
                "archive": f"runs/{name}",
                "report_sha256": "a" * 64,
                "claims": [dict(claim)],
            }
            for name in critic_runner.RUN_NAMES
        }
        scorecard = critic_runner.pairing_scorecard(runs)
        blind, key = critic_runner.create_blind_bundle(scorecard, "seed-one")

        modified_claims = json.loads(json.dumps(blind))
        modified_claims["runs"]["R01"]["claims"][0]["claim"] = "changed"
        with self.assertRaisesRegex(critic_runner.ScorecardError, "claims were modified"):
            critic_runner.apply_blind_pairings(scorecard, modified_claims, key)

        _, wrong_key = critic_runner.create_blind_bundle(scorecard, "seed-two")
        with self.assertRaisesRegex(critic_runner.ScorecardError, "do not match"):
            critic_runner.apply_blind_pairings(scorecard, blind, wrong_key)

        unknown_pair = json.loads(json.dumps(blind))
        first_comparison = next(iter(unknown_pair["comparisons"].values()))
        first_comparison["pairs"] = [
            {"left": "A999", "right": "A1", "classification": "overlap"}
        ]
        with self.assertRaisesRegex(critic_runner.ScorecardError, "not a claim"):
            critic_runner.apply_blind_pairings(scorecard, unknown_pair, key)

        with self.assertRaisesRegex(critic_runner.ScorecardError, "blind seed"):
            critic_runner.create_blind_bundle(scorecard, "line\nbreak")

    def test_extract_critic_claims_preserves_a_item_provenance(self) -> None:
        self.assertEqual(
            critic_runner.extract_critic_claims(VALID_REPORT),
            [
                {
                    "id": "A1",
                    "position": "“原文”",
                    "claim": "缺少关键一步。",
                    "reason": "理由。",
                }
            ],
        )

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

    def test_timeout_terminates_executor_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "descendant-survived"
            descendant_code = (
                "import pathlib,time; time.sleep(0.8); "
                f"pathlib.Path({str(marker)!r}).write_text('alive')"
            )
            parent_code = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}]); "
                "time.sleep(5)"
            )
            result = critic_runner.execute_with_limits(
                [sys.executable, "-c", parent_code],
                b"prompt",
                timeout_seconds=0.2,
                max_output_bytes=1024,
                capture_dir=root,
            )
            self.assertTrue(result.timed_out)
            time.sleep(1.0)
            self.assertFalse(marker.exists(), "executor descendant survived timeout")

    def test_normal_executor_exit_does_not_leave_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "descendant-survived"
            descendant_code = (
                "import pathlib,time; time.sleep(0.8); "
                f"pathlib.Path({str(marker)!r}).write_text('alive')"
            )
            parent_code = (
                "import subprocess,sys; "
                f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}])"
            )
            result = critic_runner.execute_with_limits(
                [sys.executable, "-c", parent_code],
                b"prompt",
                timeout_seconds=2,
                max_output_bytes=1024,
                capture_dir=root,
            )
            self.assertFalse(result.timed_out)
            time.sleep(1.0)
            self.assertFalse(marker.exists(), "executor descendant survived parent exit")

    def test_posix_cleanup_kills_group_even_after_parent_exit(self) -> None:
        process = Mock(pid=4321)
        process.poll.return_value = 0
        with patch.object(critic_execution.os, "name", "posix"), patch.object(
            critic_execution.os, "killpg", create=True
        ) as killpg, patch.object(
            critic_execution.signal, "SIGKILL", 9, create=True
        ):
            critic_execution._terminate_process_tree(process)
        killpg.assert_called_once_with(4321, 9)

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
                order_seed="test-seed",
                executor_label="fixture-model; temperature=0",
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
            self.assertEqual(manifest["schema_version"], 3)
            self.assertEqual(
                manifest["executor"]["label"], "fixture-model; temperature=0"
            )
            self.assertEqual(
                manifest["protocols"],
                ["critic-individualist", "critic-contrastivist"],
            )
            self.assertEqual(len(manifest["runs"]), 4)
            expected_order = [
                f"{critic_runner.PROTOCOL_PREFIX[protocol]}{repetition}"
                for protocol, repetition in critic_runner.campaign_schedule(
                    manifest["protocols"], 2, "test-seed"
                )
            ]
            self.assertEqual(manifest["execution_order"], expected_order)
            self.assertEqual(
                [run["label"] for run in manifest["runs"]], expected_order
            )
            self.assertTrue((campaign_dir / "scorecard.json").is_file())
            self.assertTrue((campaign_dir / "SUMMARY.md").is_file())
            summary_text = (campaign_dir / "SUMMARY.md").read_text(encoding="utf-8")
            self.assertIn("blind-scorecard", summary_text)
            self.assertIn("apply-blind-scorecard", summary_text)
            self.assertEqual(len(list((campaign_dir / "runs").iterdir())), 4)
            generated_scorecard = json.loads(
                (campaign_dir / "scorecard.json").read_text(encoding="utf-8")
            )
            self.assertEqual(generated_scorecard["schema_version"], 3)
            self.assertEqual(
                generated_scorecard["run_order"], ["I1", "I2", "C1", "C2"]
            )
            self.assertEqual(
                generated_scorecard["runs"]["I1"]["claims"][0]["id"], "A1"
            )
            verification = critic_runner.verify_campaign_dir(campaign_dir, source)
            self.assertTrue(verification.valid, verification.errors)

            scorecard_path = campaign_dir / "scorecard.json"
            blind_path = campaign_dir / "blind-review.json"
            blind_key_path = campaign_dir / "blind-key.json"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.blind_scorecard_command(
                        argparse.Namespace(
                            scorecard=str(scorecard_path),
                            output=None,
                            key_output=None,
                            seed="command-seed",
                        )
                    ),
                    0,
                )
            blind_text = blind_path.read_text(encoding="utf-8")
            self.assertNotIn("critic-individualist", blind_text)
            self.assertNotIn("critic-contrastivist", blind_text)
            blind_scorecard = json.loads(blind_text)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.blind_scorecard_command(
                        argparse.Namespace(
                            scorecard=str(scorecard_path),
                            output=None,
                            key_output=None,
                            seed="command-seed",
                        )
                    ),
                    critic_runner.EXIT_INVALID_SCORECARD,
                )
            for comparison in blind_scorecard["comparisons"].values():
                comparison["complete"] = True
                comparison["pairs"] = [
                    {"left": "A1", "right": "A1", "classification": "overlap"}
                ]
            blind_path.write_text(json.dumps(blind_scorecard), encoding="utf-8")
            merged_path = campaign_dir / "completed-scorecard.json"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.apply_blind_scorecard_command(
                        argparse.Namespace(
                            scorecard=str(scorecard_path),
                            blind=None,
                            key=None,
                            output=None,
                        )
                    ),
                    0,
                )
            merged_scorecard = json.loads(merged_path.read_text(encoding="utf-8"))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.apply_blind_scorecard_command(
                        argparse.Namespace(
                            scorecard=str(scorecard_path),
                            blind=None,
                            key=None,
                            output=None,
                        )
                    ),
                    critic_runner.EXIT_INVALID_SCORECARD,
                )
            self.assertEqual(
                critic_runner.score_divergence(merged_scorecard)["verdict"], "reject"
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.score_command(
                        argparse.Namespace(
                            scorecard=str(merged_path),
                            format="json",
                            output=None,
                        )
                    ),
                    0,
                )
            with redirect_stderr(io.StringIO()):
                self.assertEqual(
                    critic_runner.apply_blind_scorecard_command(
                        argparse.Namespace(
                            scorecard=str(scorecard_path),
                            blind=str(blind_path),
                            key=str(blind_key_path),
                            output=str(root / "detached-scorecard.json"),
                        )
                    ),
                    critic_runner.EXIT_INVALID_SCORECARD,
                )

            scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))
            scorecard["margin"] = 0.3
            scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
            filled = critic_runner.verify_campaign_dir(campaign_dir, source)
            self.assertTrue(filled.valid, filled.errors)
            self.assertTrue(any("filled" in warning for warning in filled.warnings))

            scorecard["runs"]["I1"]["claims"][0]["claim"] = "tampered"
            scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
            provenance_failure = critic_runner.verify_campaign_dir(campaign_dir, source)
            self.assertFalse(provenance_failure.valid)
            self.assertTrue(
                any("claims do not match" in error for error in provenance_failure.errors)
            )
            scorecard["runs"]["I1"]["claims"][0]["claim"] = "缺少关键一步。"
            scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")

            scorecard["runs"]["I1"]["protocol"] = "critic-natural-science"
            scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
            identity_failure = critic_runner.verify_campaign_dir(campaign_dir, source)
            self.assertFalse(identity_failure.valid)
            self.assertTrue(
                any("protocol does not match" in error for error in identity_failure.errors)
            )
            scorecard["runs"]["I1"]["protocol"] = "critic-individualist"

            scorecard["run_order"] = list(reversed(scorecard["run_order"]))
            scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
            order_identity_failure = critic_runner.verify_campaign_dir(
                campaign_dir, source
            )
            self.assertFalse(order_identity_failure.valid)
            self.assertTrue(
                any(
                    "run_order does not match campaign" in error
                    for error in order_identity_failure.errors
                )
            )
            scorecard["run_order"] = ["I1", "I2", "C1", "C2"]
            scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")

            scorecard["comparisons"]["I1:I2"]["pairs"] = [
                {"left": "A999", "right": "A1", "classification": "overlap"}
            ]
            scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
            malformed_draft = critic_runner.verify_campaign_dir(campaign_dir, source)
            self.assertFalse(malformed_draft.valid)
            self.assertTrue(
                any("structure is invalid" in error for error in malformed_draft.errors)
            )
            scorecard["comparisons"]["I1:I2"]["pairs"] = []
            scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")

            campaign_manifest_path = campaign_dir / "campaign.json"
            original_campaign_manifest = campaign_manifest_path.read_text(encoding="utf-8")
            incomplete_manifest = json.loads(original_campaign_manifest)
            incomplete_manifest["runs"].pop()
            campaign_manifest_path.write_text(
                json.dumps(incomplete_manifest), encoding="utf-8"
            )
            incomplete = critic_runner.verify_campaign_dir(campaign_dir, source)
            self.assertFalse(incomplete.valid)
            self.assertTrue(any("run matrix mismatch" in error for error in incomplete.errors))
            campaign_manifest_path.write_text(
                original_campaign_manifest, encoding="utf-8"
            )

            reordered_manifest = json.loads(original_campaign_manifest)
            reordered_manifest["execution_order"] = list(
                reversed(reordered_manifest["execution_order"])
            )
            campaign_manifest_path.write_text(
                json.dumps(reordered_manifest), encoding="utf-8"
            )
            reordered = critic_runner.verify_campaign_dir(campaign_dir, source)
            self.assertFalse(reordered.valid)
            self.assertTrue(
                any("execution_order" in error for error in reordered.errors)
            )
            campaign_manifest_path.write_text(
                original_campaign_manifest, encoding="utf-8"
            )

            first_run = campaign_dir / manifest["runs"][0]["run_dir"]
            (first_run / "report.md").write_bytes(b"corrupt")
            corrupted = critic_runner.verify_campaign_dir(campaign_dir, source)
            self.assertFalse(corrupted.valid)
            self.assertTrue(any("hash mismatch" in error for error in corrupted.errors))

    def test_cross_track_campaign_creates_a_scoreable_dynamic_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "draft.md"
            source.write_text("draft", encoding="utf-8")
            encoded_report = base64.b64encode(VALID_REPORT.encode("utf-8")).decode()
            args = argparse.Namespace(
                manuscript=str(source),
                protocol=None,
                track=[
                    "humanities-social-science",
                    "natural-science",
                    "engineering",
                ],
                repeat=2,
                order_seed="cross-track-seed",
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
            scorecard_path = campaign_dir / "scorecard.json"
            scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))
            self.assertEqual(scorecard["schema_version"], 3)
            self.assertEqual(
                scorecard["run_order"], ["S1", "S2", "N1", "N2", "E1", "E2"]
            )
            self.assertEqual(len(scorecard["comparisons"]), 15)
            for comparison in scorecard["comparisons"].values():
                comparison["complete"] = True
                comparison["pairs"] = [
                    {"left": "A1", "right": "A1", "classification": "overlap"}
                ]
            scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
            result = critic_runner.score_divergence(scorecard)
            self.assertEqual(len(result["within_comparisons"]), 3)
            self.assertEqual(len(result["between_comparisons"]), 12)
            verification = critic_runner.verify_campaign_dir(campaign_dir, source)
            self.assertTrue(verification.valid, verification.errors)

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
                    "complete": True,
                    "pairs": [
                        {
                            "left": "A1",
                            "right": "A1",
                            "classification": "overlap",
                        }
                    ],
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
