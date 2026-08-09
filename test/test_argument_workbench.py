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

import argument_workbench as workbench  # noqa: E402
import argument_ir  # noqa: E402
import critic_runner  # noqa: E402


FIXTURE = REPO_ROOT / "test" / "fixtures" / "workbench-demo"


class ArgumentWorkbenchTests(unittest.TestCase):
    def make_project(self, root: Path) -> workbench.WorkspacePaths:
        return workbench.initialize_workspace(
            FIXTURE / "manuscript.md",
            root / "结构的替身.argument-workbench",
            title="结构的替身",
        )

    def collect_fixture(self, paths: workbench.WorkspacePaths) -> dict[str, object]:
        _, record = workbench.collect_raw_attempt(
            paths.root,
            (FIXTURE / "raw-ir.json").read_bytes(),
            method="file",
            source_name="raw-ir.json",
            producer_label="fixture-model",
        )
        return record

    def test_init_collect_rebuild_and_verify_are_byte_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_project(Path(temporary))
            self.assertTrue(paths.prompt.is_file())
            self.assertEqual(workbench.verify_workspace(paths, allow_incomplete=True), [])
            record = self.collect_fixture(paths)
            self.assertEqual(record["validation"]["status"], "valid")
            map_path, changed = workbench.rebuild_workspace(paths.root)
            self.assertTrue(changed)
            first = (
                paths.reviewed_payload.read_bytes(),
                paths.reviewed_record.read_bytes(),
                map_path.read_bytes(),
            )
            _, changed_again = workbench.rebuild_workspace(paths.root)
            self.assertFalse(changed_again)
            self.assertEqual(
                first,
                (
                    paths.reviewed_payload.read_bytes(),
                    paths.reviewed_record.read_bytes(),
                    map_path.read_bytes(),
                ),
            )
            self.assertEqual(workbench.verify_workspace(paths), [])
            markdown = map_path.read_text(encoding="utf-8")
            self.assertIn("## Core Claims", markdown)
            self.assertIn("[deterministic]", markdown)
            self.assertNotIn("Argument Score", markdown)

    def test_new_workspace_uses_classification_focused_extraction_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_project(Path(temporary))
            prompt = paths.prompt.read_text(encoding="utf-8")
            self.assertIn("Protocol: argument-ir-extraction-v2", prompt)
            self.assertIn("types 和 methods 默认各选择一个最主要值", prompt)
            self.assertIn("methods 描述实际支撑该 Claim 的方法", prompt)
            self.assertIn("不要用 Claim 自身的重复表述冒充 Evidence", prompt)
            self.assertEqual(
                workbench.verify_workspace(paths, allow_incomplete=True), []
            )

    def test_legacy_extraction_prompt_remains_byte_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_project(Path(temporary))
            source_bytes = (FIXTURE / "manuscript.md").read_bytes()
            legacy = argument_ir.build_ir_extraction_prompt(
                source_bytes.decode("utf-8-sig"),
                source_name="manuscript.md",
                source_sha256=workbench.sha256_bytes(source_bytes),
                protocol_version=1,
            ).encode("utf-8")
            self.assertEqual(
                workbench.sha256_bytes(legacy),
                "c190846f856b54d33e4bf90ba9dde2bc75eb4e4be278bbc487b26d8350683976",
            )
            paths.prompt.write_bytes(legacy)
            self.assertEqual(
                workbench.verify_workspace(paths, allow_incomplete=True), []
            )
            record = self.collect_fixture(paths)
            self.assertEqual(record["prompt_sha256"], workbench.sha256_bytes(legacy))
            workbench.rebuild_workspace(paths.root)
            self.assertEqual(workbench.verify_workspace(paths), [])

            paths.prompt.write_bytes(legacy + b"\n")
            errors = workbench.verify_workspace(paths)
            self.assertTrue(
                any("supported deterministic source-bound prompt" in error for error in errors)
            )
            self.assertTrue(any("prompt SHA-256 mismatch" in error for error in errors))

    def test_init_preserves_utf8_bom_and_crlf_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manuscript = root / "draft with spaces.md"
            exact = b"\xef\xbb\xbfFirst claim.\r\nSecond claim.\r\n"
            manuscript.write_bytes(exact)
            paths = workbench.initialize_workspace(
                manuscript,
                root / "draft.argument-workbench",
            )
            version = json.loads(paths.version.read_text(encoding="utf-8"))
            archived = paths.version_dir / version["source"]["relative_path"]
            self.assertEqual(archived.read_bytes(), exact)
            self.assertEqual(version["source"]["sha256"], workbench.sha256_bytes(exact))
            self.assertEqual(workbench.verify_workspace(paths, allow_incomplete=True), [])

    def test_init_recovers_same_project_but_refuses_different_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self.make_project(root)
            repeated = self.make_project(root)
            self.assertEqual(repeated.root, paths.root)
            other_dir = root / "other"
            other_dir.mkdir()
            other = other_dir / "manuscript.md"
            other.write_text("A different manuscript.", encoding="utf-8")
            with self.assertRaisesRegex(workbench.WorkbenchError, "different manuscript bytes"):
                workbench.initialize_workspace(other, paths.root)

    def test_invalid_attempt_is_preserved_and_next_valid_attempt_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_project(Path(temporary))
            invalid_path, invalid = workbench.collect_raw_attempt(
                paths.root,
                b'{"artifact":"argument-ir","artifact":"duplicate"}\n',
                method="terminal-paste",
                source_name="pasted-argument-ir.json",
                producer_label=None,
            )
            self.assertEqual(invalid["validation"]["status"], "unusable")
            self.assertEqual((invalid_path / "response.json").read_bytes(), b'{"artifact":"argument-ir","artifact":"duplicate"}\n')
            valid_path, valid = workbench.collect_raw_attempt(
                paths.root,
                (FIXTURE / "raw-ir.json").read_bytes(),
                method="file",
                source_name="raw-ir.json",
                producer_label="fixture-model",
            )
            self.assertEqual(valid["validation"]["status"], "valid")
            self.assertEqual(valid_path.name, "attempt-0002")
            selected, _, _ = workbench.selected_attempt(paths)
            self.assertEqual(selected, valid_path)
            newer_path, _ = workbench.collect_raw_attempt(
                paths.root,
                (FIXTURE / "raw-ir.json").read_bytes(),
                method="file",
                source_name="second-valid.json",
                producer_label="fixture-model-2",
            )
            selected, _, _ = workbench.selected_attempt(paths)
            self.assertEqual(selected, newer_path)
            workbench.append_correction(
                paths.root,
                {
                    "kind": "update_node",
                    "target": "raw:C1",
                    "changes": {"uncertainty": "作者需要判断‘总会’的强度"},
                },
            )
            later_path, _ = workbench.collect_raw_attempt(
                paths.root,
                (FIXTURE / "raw-ir.json").read_bytes(),
                method="file",
                source_name="third-valid.json",
                producer_label="fixture-model-3",
            )
            self.assertNotEqual(later_path, newer_path)
            selected, _, _ = workbench.selected_attempt(paths)
            self.assertEqual(selected, newer_path)

    def test_correctable_model_semantics_can_be_fixed_without_editing_raw_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_project(Path(temporary))
            raw = json.loads((FIXTURE / "raw-ir.json").read_text(encoding="utf-8"))
            raw["claims"][0]["types"] = ["not-a-claim-type"]
            raw_bytes = (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            _, attempt = workbench.collect_raw_attempt(
                paths.root,
                raw_bytes,
                method="file",
                source_name="bad-types.json",
                producer_label="fixture-model",
            )
            self.assertEqual(attempt["validation"]["status"], "correctable")
            with self.assertRaisesRegex(workbench.WorkbenchError, "not yet valid"):
                workbench.rebuild_workspace(paths.root)
            workbench.append_correction(
                paths.root,
                {
                    "kind": "update_node",
                    "target": "raw:C1",
                    "changes": {"types": ["causal"]},
                },
                reason="模型把机制性主张写成未知类型",
            )
            workbench.rebuild_workspace(paths.root)
            reviewed = json.loads(paths.reviewed_payload.read_text(encoding="utf-8"))
            record = json.loads(paths.reviewed_record.read_text(encoding="utf-8"))
            self.assertEqual(reviewed["claims"][0]["types"], ["causal"])
            self.assertEqual(
                record["field_provenance"]["C1.types"],
                {"origin": "human-confirmed", "source": "IC0001"},
            )
            self.assertEqual(raw["claims"][0]["types"], ["not-a-claim-type"])

    def test_replay_add_remove_rebind_and_revert_preserves_stable_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_project(Path(temporary))
            self.collect_fixture(paths)
            raw_response = paths.raw_dir / "attempt-0001" / "response.json"
            original_raw_bytes = raw_response.read_bytes()
            workbench.append_correction(
                paths.root,
                {
                    "kind": "update_relation",
                    "target": "raw:R1",
                    "changes": {"to": "raw:C1"},
                },
                reason="案例证据支持经验概括，不支持概念定义",
            )
            add_path, added = workbench.append_correction(
                paths.root,
                {
                    "kind": "add_node",
                    "node_kind": "claim",
                    "node": {
                        "text": "公共争议最初针对制度安排",
                        "source_quote": "平台上的公共争议往往从制度安排开始，却以某个具体人物的品格判断结束。",
                        "types": ["descriptive"],
                        "methods": ["descriptive-empirical"],
                        "role": "premise",
                        "extraction": "explicit",
                        "uncertainty": "",
                    },
                },
                reason="补入模型漏掉的前提",
            )
            self.assertEqual(add_path.name, "IC0002.json")
            self.assertEqual(added["correction_id"], "IC0002")
            workbench.append_correction(
                paths.root,
                {
                    "kind": "add_relation",
                    "relation": {
                        "type": "supports",
                        "from": "correction:IC0002",
                        "to": "raw:C3",
                    },
                },
            )
            workbench.append_correction(
                paths.root,
                {"kind": "remove_node", "target": "raw:E2"},
                reason="E2 与 E1 重复",
            )
            workbench.rebuild_workspace(paths.root)
            reviewed = json.loads(paths.reviewed_payload.read_text(encoding="utf-8"))
            self.assertEqual(len(reviewed["claims"]), 4)
            self.assertEqual(len(reviewed["evidence"]), 1)
            self.assertFalse(any(item["from"] == "E2" for item in reviewed["relations"]))
            record = json.loads(paths.reviewed_record.read_text(encoding="utf-8"))
            self.assertEqual(record["stable_ref_map"]["correction:IC0002"], "C4")
            self.assertIn("removed.raw:E2", record["field_provenance"])

            workbench.append_correction(
                paths.root,
                {"kind": "revert_correction", "target": "IC0004"},
                reason="保留第二个案例观察",
            )
            workbench.rebuild_workspace(paths.root)
            restored = json.loads(paths.reviewed_payload.read_text(encoding="utf-8"))
            self.assertEqual(len(restored["evidence"]), 2)
            self.assertTrue(any(item["from"] == "E2" for item in restored["relations"]))
            self.assertEqual(raw_response.read_bytes(), original_raw_bytes)
            self.assertEqual(workbench.verify_workspace(paths), [])

    def test_scripted_line_inspector_writes_formal_correction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_project(Path(temporary))
            self.collect_fixture(paths)
            workbench.rebuild_workspace(paths.root)
            answers = iter(["e", "C1", "types", "causal", "q"])
            output: list[str] = []
            exit_code = workbench.run_inspector(
                paths.root,
                view_only=False,
                input_fn=lambda _: next(answers),
                output_fn=output.append,
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue((paths.corrections_dir / "IC0001.json").is_file())
            reviewed = json.loads(paths.reviewed_payload.read_text(encoding="utf-8"))
            self.assertEqual(reviewed["claims"][0]["types"], ["causal"])
            self.assertTrue(any("Correction saved" in line for line in output))

    def test_classification_triage_writes_one_confirmed_event_and_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_project(Path(temporary))
            self.collect_fixture(paths)
            workbench.rebuild_workspace(paths.root)
            answers = iter(
                [
                    "c",
                    "e",
                    "causal",
                    "causal-observational",
                    "y",
                    "The wording asserts a directional empirical mechanism.",
                    "q",
                    "q",
                ]
            )
            output: list[str] = []
            self.assertEqual(
                workbench.run_inspector(
                    paths.root,
                    view_only=False,
                    input_fn=lambda _: next(answers),
                    output_fn=output.append,
                ),
                0,
            )
            corrections = workbench.correction_entries(paths)
            self.assertEqual(len(corrections), 1)
            self.assertEqual(
                corrections[0][1]["operation"]["changes"],
                {
                    "types": ["causal"],
                    "methods": ["causal-observational"],
                },
            )
            reviewed = json.loads(paths.reviewed_payload.read_text(encoding="utf-8"))
            record = json.loads(paths.reviewed_record.read_text(encoding="utf-8"))
            self.assertEqual(reviewed["claims"][0]["types"], ["causal"])
            self.assertEqual(
                reviewed["claims"][0]["methods"], ["causal-observational"]
            )
            self.assertEqual(
                record["field_provenance"]["C1.types"],
                {"origin": "human-confirmed", "source": "IC0001"},
            )
            self.assertEqual(
                record["field_provenance"]["C1.methods"],
                {"origin": "human-confirmed", "source": "IC0001"},
            )
            self.assertTrue(
                any("Correction saved immediately: IC0001.json" in line for line in output)
            )
            self.assertEqual(workbench.verify_workspace(paths), [])

    def test_classification_triage_cancellation_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_project(Path(temporary))
            self.collect_fixture(paths)
            workbench.rebuild_workspace(paths.root)
            answers = iter(
                [
                    "c",
                    "e",
                    "causal",
                    "causal-observational",
                    "n",
                    "q",
                    "q",
                ]
            )
            output: list[str] = []
            self.assertEqual(
                workbench.run_inspector(
                    paths.root,
                    view_only=False,
                    input_fn=lambda _: next(answers),
                    output_fn=output.append,
                ),
                0,
            )
            self.assertEqual(workbench.correction_entries(paths), [])
            self.assertTrue(any("Cancelled; model-derived values kept." in line for line in output))
            self.assertEqual(workbench.verify_workspace(paths), [])

    def test_workspace_rejects_tampered_source_and_derived_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_project(Path(temporary))
            self.collect_fixture(paths)
            workbench.rebuild_workspace(paths.root)
            paths.argument_map.write_text("tampered", encoding="utf-8")
            self.assertTrue(any("argument-map.md" in error for error in workbench.verify_workspace(paths)))
            workbench.rebuild_workspace(paths.root)
            version = json.loads(paths.version.read_text(encoding="utf-8"))
            source = paths.version_dir / version["source"]["relative_path"]
            source.write_bytes(source.read_bytes() + b"tamper")
            self.assertTrue(any("source hash" in error for error in workbench.verify_workspace(paths)))

    def test_verify_recomputes_raw_attempt_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_project(Path(temporary))
            self.collect_fixture(paths)
            record_path = paths.raw_dir / "attempt-0001" / "record.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["validation"] = {
                "status": "correctable",
                "errors": ["invented validation result"],
            }
            record_path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(
                any(
                    "fresh Raw IR validation" in error
                    for error in workbench.verify_workspace(paths, allow_incomplete=True)
                )
            )

    def test_new_cli_runs_end_to_end_with_paths_containing_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace with spaces"
            project = root / "demo project.argument-workbench"
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "init",
                            str(FIXTURE / "manuscript.md"),
                            "--project-dir",
                            str(project),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "collect",
                            str(project),
                            "--file",
                            str(FIXTURE / "raw-ir.json"),
                            "--producer-label",
                            "fixture-model",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(["ir", "inspect", str(project), "--view-only"]),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(["ir", "rebuild", str(project)]),
                    0,
                )
                version_dir = project / "documents" / "D1" / "versions" / "V1"
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "plan",
                            str(version_dir / "source" / "manuscript.md"),
                            str(version_dir / "reviewed-ir" / "argument-ir.json"),
                            "--output",
                            str(root / "reviewed-plan.json"),
                            "--prompt-output",
                            str(root / "reviewed-plan-prompt.md"),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    critic_runner.main(["ir", "verify-project", str(project)]),
                    0,
                )
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("Validation status: valid", stdout.getvalue())
            self.assertTrue((project / "documents" / "D1" / "versions" / "V1" / "reviewed-ir" / "argument-map.md").is_file())

    def test_cli_paste_preserves_normalized_attempt_and_non_tty_requires_view_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_project(Path(temporary))
            pasted = (FIXTURE / "raw-ir.json").read_text(encoding="utf-8").rstrip() + "\n::END::\n"
            with patch("sys.stdin", StringIO(pasted)), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    critic_runner.main(
                        [
                            "ir",
                            "collect",
                            str(paths.root),
                            "--paste",
                            "--producer-label",
                            "pasted-model",
                        ]
                    ),
                    0,
                )
            response = paths.raw_dir / "attempt-0001" / "response.json"
            self.assertTrue(response.read_bytes().endswith(b"\n"))
            with patch("sys.stdin", StringIO("")), redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
                self.assertEqual(
                    critic_runner.main(["ir", "inspect", str(paths.root)]),
                    2,
                )
            self.assertIn("requires a terminal", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
