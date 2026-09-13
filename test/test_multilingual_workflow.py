"""Exercise legacy encodings beyond ingestion, through approval and export."""
import base64
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

import critic_runner
import argument_revision as revision
from argument_app import ProductApp, create_uploaded_project
from argument_workbench import workspace_paths, verify_project_versions
from test import test_argument_revision
from test.test_multilingual_import import SAMPLES


class MultilingualWorkflowTests(unittest.TestCase):
    def test_browser_api_to_human_revision_and_export_keeps_eight_languages(self):
        fixture = test_argument_revision.ArgumentRevisionTests()
        for name, (sample, encoding) in SAMPLES.items():
            with self.subTest(language=name), tempfile.TemporaryDirectory() as temp:
                text = fixture.SOURCE + "\n" + sample + "\n"
                raw = text.encode(encoding)
                app = ProductApp.create(Path(temp) / "library").import_manuscript({
                    "filename": name + ".md", "title": sample,
                    "content_base64": base64.b64encode(raw).decode("ascii"),
                    "encoding": encoding,
                })
                project = app.project_dir
                self.assertEqual(app.view()["selected"]["title"], sample)
                fixture.atomize(project)
                result, _, _ = fixture.proposal(project)
                self.assertTrue(result.valid, result.errors)
                revision.append_hunk_decision(project, "CH1", decision="accept", reason="Checked the exact change")
                revision.apply_approved_hunks(project)
                run = revision.prepare_resolution_review(project)
                record = json.loads((run / "record.json").read_text(encoding="utf-8"))
                response = {"schema_version": 1, "resolution_run_id": record["resolution_run_id"],
                    "manuscript_version_id": "V2", "source_sha256": record["source_sha256"],
                    "results": [{"finding_id": "F1", "proposed_status": "resolved",
                        "reason": "The approved condition appears in the new draft.",
                        "evidence_quotes": ["The bridge works in dry weather."], "uncertainties": []}]}
                result = revision.collect_resolution_result(project, json.dumps(response))
                self.assertTrue(result.valid, result.errors)
                revision.append_resolution_decision(project, "F1", status="resolved", reason="Checked revised wording")
                exported = revision.export_revision(project)
                self.assertIn(sample, (exported / "V2.md").read_text(encoding="utf-8"))
                original = next((workspace_paths(project, "V1").version_dir / "source").iterdir())
                self.assertEqual(original.read_bytes(), raw)
                self.assertEqual(verify_project_versions(project), [])
                self.assertEqual(revision.verify_revision_workflow(project), [])
                self.assertEqual(revision.workflow_view(project)["stage"], "complete")

    def test_same_bytes_with_different_decoding_do_not_reopen_other_text(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = "Русский текст".encode("cp1251")
            one = create_uploaded_project(temp, filename="draft.txt", content=raw, encoding="cp1251")
            two = create_uploaded_project(temp, filename="draft.txt", content=raw, encoding="cp1252")
            self.assertNotEqual(one, two)
            self.assertEqual(create_uploaded_project(temp, filename="draft.txt", content=raw, encoding="windows-1251"), one)

    def test_cli_ir_encoding_reaches_version_import_and_preparation(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(StringIO()):
            root = Path(temp)
            source = root / "draft.txt"
            source.write_bytes("繁體中文論證".encode("big5"))
            project = root / "project"
            self.assertEqual(critic_runner.main(["ir", "init", str(source), "--project-dir", str(project), "--encoding", "big5"]), 0)
            output = root / "prompt.md"
            self.assertEqual(critic_runner.main(["ir", "prepare", str(source), "--output", str(output), "--encoding", "big5"]), 0)
            self.assertIn("繁體中文論證", output.read_text(encoding="utf-8"))
            source.write_bytes("日本語の第二版".encode("cp932"))
            self.assertEqual(critic_runner.main(["ir", "import-version", str(project), str(source), "--encoding", "cp932"]), 0)
            self.assertEqual(verify_project_versions(project), [])


if __name__ == "__main__":
    unittest.main()
